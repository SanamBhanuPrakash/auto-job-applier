"""Fan out across every configured discovery source, normalize, dedupe, persist."""
from __future__ import annotations

import logging

import httpx
from sqlalchemy import select

from jobbot.config import load_companies, load_search_settings
from jobbot.db import session_scope
from jobbot.discovery import adzuna, ashby, greenhouse, lever, recruitee, remoteok, remotive, smartrecruiters, usajobs
from jobbot.discovery.base import NormalizedJob
from jobbot.discovery.recency import filter_recent
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
    """Each aggregator can be configured with either a single query (the
    original what/where/tag/category keys, kept working for anyone with an
    existing settings.yaml) or a LIST of them (queries/tags/categories) —
    e.g. one Adzuna query per city plus a remote-worldwide one, or a
    RemoteOK/Remotive tag per role you're searching for. Fans out over all
    of them; results overlapping across queries dedupe naturally in
    persist_jobs since Adzuna/RemoteOK/Remotive ids are stable regardless
    of which query surfaced a given posting."""
    settings = load_search_settings()
    agg_cfg = settings.get("aggregators", {}) or {}
    jobs: list[NormalizedJob] = []

    with httpx.Client() as client:
        adzuna_cfg = agg_cfg.get("adzuna", {}) or {}
        if adzuna_cfg.get("enabled"):
            queries = adzuna_cfg.get("queries") or [
                {"what": adzuna_cfg.get("what", ""), "where": adzuna_cfg.get("where", ""), "country": adzuna_cfg.get("country", "")}
            ]
            for q in queries:
                what, where, country = q.get("what", ""), q.get("where", ""), q.get("country", "")
                log.info("Discovering adzuna / what=%r where=%r country=%r", what, where, country)
                jobs.extend(adzuna.fetch_jobs(what, where, country, client=client))

        usajobs_cfg = agg_cfg.get("usajobs", {}) or {}
        if usajobs_cfg.get("enabled"):
            jobs.extend(usajobs.fetch_jobs(usajobs_cfg.get("keyword", ""), client=client))

        remoteok_cfg = agg_cfg.get("remoteok", {}) or {}
        if remoteok_cfg.get("enabled"):
            tags = remoteok_cfg.get("tags") or [remoteok_cfg.get("tag", "")]
            for tag in tags:
                log.info("Discovering remoteok / tag=%r", tag)
                jobs.extend(remoteok.fetch_jobs(tag, client=client))

        remotive_cfg = agg_cfg.get("remotive", {}) or {}
        if remotive_cfg.get("enabled"):
            categories = remotive_cfg.get("categories") or [remotive_cfg.get("category", "")]
            for category in categories:
                log.info("Discovering remotive / category=%r", category)
                jobs.extend(remotive.fetch_jobs(category, client=client))

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


def run_discovery(max_age_days: float | None = None) -> tuple[int, int]:
    """max_age_days: only keep postings from roughly this many days back
    (a posting with no parseable date is kept regardless — see recency.py).
    None uses config/settings.yaml's search.posted_within_days; 0 or that
    key being absent disables the filter entirely."""
    jobs = discover_ats_jobs() + discover_aggregator_jobs()
    log.info("Discovered %d raw postings before dedupe", len(jobs))

    if max_age_days is None:
        search_cfg = load_search_settings().get("search", {}) or {}
        max_age_days = search_cfg.get("posted_within_days", 0)
    if max_age_days:
        jobs = filter_recent(jobs, max_age_days)

    return persist_jobs(jobs)
