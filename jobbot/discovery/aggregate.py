"""Fan out across every configured discovery source, normalize, dedupe, persist."""
from __future__ import annotations

import logging

import httpx
from sqlalchemy import select

from jobbot.config import load_companies, load_search_settings
from jobbot.db import session_scope
from jobbot.discovery import adzuna, ashby, greenhouse, lever, recruitee, remoteok, remotive, smartrecruiters, usajobs
from jobbot.discovery.base import NormalizedJob
from jobbot.models import Job

log = logging.getLogger(__name__)

_ATS_CONNECTORS = {
    "greenhouse": greenhouse.fetch_jobs,
    "lever": lever.fetch_jobs,
    "ashby": ashby.fetch_jobs,
    "smartrecruiters": smartrecruiters.fetch_jobs,
    "recruitee": recruitee.fetch_jobs,
}


def discover_ats_jobs() -> list[NormalizedJob]:
    """Query every company slug listed in config/companies.yaml across each ATS."""
    companies = load_companies()
    jobs: list[NormalizedJob] = []
    with httpx.Client() as client:
        for ats, fetch in _ATS_CONNECTORS.items():
            slugs = companies.get(ats, []) or []
            for slug in slugs:
                log.info("Discovering %s / %s", ats, slug)
                jobs.extend(fetch(slug, client=client))
    return jobs


def discover_aggregator_jobs() -> list[NormalizedJob]:
    settings = load_search_settings()
    agg_cfg = settings.get("aggregators", {}) or {}
    jobs: list[NormalizedJob] = []

    with httpx.Client() as client:
        adzuna_cfg = agg_cfg.get("adzuna", {}) or {}
        if adzuna_cfg.get("enabled"):
            jobs.extend(adzuna.fetch_jobs(adzuna_cfg.get("what", ""), adzuna_cfg.get("where", ""), client=client))

        usajobs_cfg = agg_cfg.get("usajobs", {}) or {}
        if usajobs_cfg.get("enabled"):
            jobs.extend(usajobs.fetch_jobs(usajobs_cfg.get("keyword", ""), client=client))

        remoteok_cfg = agg_cfg.get("remoteok", {}) or {}
        if remoteok_cfg.get("enabled"):
            jobs.extend(remoteok.fetch_jobs(remoteok_cfg.get("tag", ""), client=client))

        remotive_cfg = agg_cfg.get("remotive", {}) or {}
        if remotive_cfg.get("enabled"):
            jobs.extend(remotive.fetch_jobs(remotive_cfg.get("category", ""), client=client))

    return jobs


def persist_jobs(jobs: list[NormalizedJob]) -> tuple[int, int]:
    """Upsert by (source, external_id). Returns (inserted, skipped_existing)."""
    inserted = skipped = 0
    with session_scope() as session:
        for nj in jobs:
            if not nj.external_id:
                continue
            existing = session.execute(
                select(Job).where(Job.source == nj.source, Job.external_id == nj.external_id)
            ).scalar_one_or_none()
            if existing is not None:
                skipped += 1
                continue
            session.add(
                Job(
                    source=nj.source,
                    external_id=nj.external_id,
                    company=nj.company,
                    title=nj.title,
                    location=nj.location,
                    remote=nj.remote,
                    url=nj.url,
                    description=nj.description,
                    posted_at=nj.posted_at,
                    ats=nj.ats,
                    raw=nj.raw,
                )
            )
            inserted += 1
    return inserted, skipped


def run_discovery() -> tuple[int, int]:
    jobs = discover_ats_jobs() + discover_aggregator_jobs()
    log.info("Discovered %d raw postings before dedupe", len(jobs))
    return persist_jobs(jobs)
