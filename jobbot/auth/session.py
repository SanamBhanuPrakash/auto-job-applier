"""Session records — what we know about our standing on each domain.

Two questions this answers before a run touches a page:

* Have we ever been signed in here, and did it work recently?
* Have we failed here repeatedly, such that trying again is more likely to
  lock the account than to succeed?

The second is the important one. `MAX_CONSECUTIVE_FAILURES` is a circuit
breaker on authentication, in the same spirit as the fill-failure breaker:
a stored credential that is wrong stays wrong, and hammering it is exactly
the behaviour that turns "wrong password" into "account locked".

No secrets are stored. See `models.AccountRecord`.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select

from jobbot.auth.credentials import normalize_domain, redact
from jobbot.auth.states import AuthOutcome
from jobbot.db import session_scope
from jobbot.models import AccountRecord

log = logging.getLogger(__name__)

#: After this many consecutive failures we stop trying that domain
#: automatically until a human intervenes.
MAX_CONSECUTIVE_FAILURES = 2

#: A verified session older than this is re-checked rather than trusted.
SESSION_TRUST_HOURS = 12


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def get_record(domain_or_url: str) -> AccountRecord | None:
    domain = normalize_domain(domain_or_url)
    if not domain:
        return None
    with session_scope() as session:
        return session.execute(
            select(AccountRecord).where(AccountRecord.domain == domain)
        ).scalar_one_or_none()


def record_attempt(domain_or_url: str, result, *, profile_dir: str = "") -> None:
    """Persist the outcome of one authentication attempt.

    `result.reason` is redacted on the way in — it can quote page text,
    and page text on a login screen is exactly where a value could appear.
    """
    domain = normalize_domain(domain_or_url)
    if not domain:
        return
    # `verified_session`, not `ok`: NOT_REQUIRED means the page never
    # asked, which an error page also does. Recording that as a verified
    # session would make `session_is_fresh` lie for the next 12 hours.
    succeeded = result.verified_session
    with session_scope() as session:
        row = session.execute(
            select(AccountRecord).where(AccountRecord.domain == domain)
        ).scalar_one_or_none()
        if row is None:
            # Column defaults are applied at flush, so a freshly
            # constructed row still has None where an int is expected.
            # Set the counter explicitly rather than relying on that.
            row = AccountRecord(domain=domain, consecutive_failures=0)
            session.add(row)
        row.last_outcome = result.outcome.value
        row.last_auth_state = result.state.value
        row.last_attempt_at = _now()
        row.note = redact(result.reason)[:500]
        if profile_dir:
            row.profile_dir = profile_dir
        if succeeded:
            row.last_verified_at = _now()
            row.consecutive_failures = 0
        elif result.outcome in (AuthOutcome.FAILED, AuthOutcome.BLOCKED):
            row.consecutive_failures = (row.consecutive_failures or 0) + 1
        session.commit()


def should_attempt(domain_or_url: str) -> tuple[bool, str]:
    """Whether it is sensible to try authenticating here right now."""
    row = get_record(domain_or_url)
    if row is None:
        return True, "no prior attempt recorded"
    failures = row.consecutive_failures or 0
    if failures >= MAX_CONSECUTIVE_FAILURES:
        return False, (
            f"{failures} consecutive failures on {row.domain}; not "
            "retrying automatically. Fix the stored credential or sign in yourself "
            f"once. Last: {row.note[:160]}"
        )
    return True, "ok"


def session_is_fresh(domain_or_url: str) -> bool:
    """True when we verified a session here recently enough to trust it.

    Freshness alone is never sufficient to skip a check — the page is
    still the source of truth, and a session can be revoked server-side at
    any moment. This only decides whether to *expect* one.
    """
    row = get_record(domain_or_url)
    if row is None or row.last_verified_at is None:
        return False
    verified = row.last_verified_at
    if verified.tzinfo is None:
        verified = verified.replace(tzinfo=dt.timezone.utc)
    return (_now() - verified) < dt.timedelta(hours=SESSION_TRUST_HOURS)


def list_records() -> list[AccountRecord]:
    with session_scope() as session:
        return list(session.execute(
            select(AccountRecord).order_by(AccountRecord.domain)
        ).scalars().all())
