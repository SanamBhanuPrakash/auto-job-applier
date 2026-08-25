import datetime as dt

from jobbot.discovery.base import NormalizedJob
from jobbot.discovery.recency import filter_recent, parse_posted_at


def test_parses_iso_with_offset():
    # Greenhouse's updated_at shape
    parsed = parse_posted_at("2026-08-01T12:00:00-00:00")
    assert parsed == dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.timezone.utc)


def test_parses_iso_with_z_suffix():
    # Remotive/RemoteOK/SmartRecruiters-style
    parsed = parse_posted_at("2026-08-01T12:00:00Z")
    assert parsed == dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.timezone.utc)


def test_parses_lever_epoch_millis_string():
    ms = 1754049600000  # 2025-08-01T12:00:00Z
    parsed = parse_posted_at(str(ms))
    assert parsed == dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc)


def test_parses_date_only_string():
    # USAJOBS-style
    parsed = parse_posted_at("2026-08-01")
    assert parsed == dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)


def test_empty_string_returns_none():
    assert parse_posted_at("") is None


def test_unparseable_garbage_returns_none():
    assert parse_posted_at("not a date at all") is None


def _job(posted_at: str, external_id: str = "1") -> NormalizedJob:
    return NormalizedJob(
        source="greenhouse", external_id=external_id, company="Acme", title="Engineer",
        url="https://example.com", posted_at=posted_at,
    )


def test_filter_recent_drops_stale_postings():
    now = dt.datetime.now(dt.timezone.utc)
    fresh = _job((now - dt.timedelta(hours=12)).isoformat(), "1")
    stale = _job((now - dt.timedelta(days=10)).isoformat(), "2")

    kept = filter_recent([fresh, stale], max_age_days=2)
    assert [j.external_id for j in kept] == ["1"]


def test_filter_recent_keeps_unparseable_dates_rather_than_dropping():
    now = dt.datetime.now(dt.timezone.utc)
    fresh = _job((now - dt.timedelta(hours=1)).isoformat(), "1")
    unknown = _job("", "2")  # source gave no date at all

    kept = filter_recent([fresh, unknown], max_age_days=2)
    assert {j.external_id for j in kept} == {"1", "2"}


def test_filter_recent_disabled_when_max_age_zero():
    now = dt.datetime.now(dt.timezone.utc)
    stale = _job((now - dt.timedelta(days=30)).isoformat(), "1")
    kept = filter_recent([stale], max_age_days=0)
    assert len(kept) == 1
