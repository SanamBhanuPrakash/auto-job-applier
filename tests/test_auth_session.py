"""Session records: no secrets stored, and a circuit breaker on retries."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from jobbot.auth import session as authsess
from jobbot.auth.orchestrator import AuthResult
from jobbot.auth.states import AuthOutcome, AuthState
from jobbot.db import session_scope
from jobbot.models import AccountRecord

DOMAIN = "record-test.example"


@pytest.fixture(autouse=True)
def clean():
    def _wipe():
        with session_scope() as s:
            for row in s.execute(
                select(AccountRecord).where(AccountRecord.domain == DOMAIN)
            ).scalars().all():
                s.delete(row)
            s.commit()

    _wipe()
    yield
    _wipe()


def _result(outcome, reason="", state=AuthState.LOGIN_REQUIRED):
    return AuthResult(outcome, state, DOMAIN, reason)


def test_the_account_record_has_no_column_for_a_secret():
    """Structural, not behavioural: this table is read by report commands,
    quoted in bug reports, and copied by whatever backs up the data dir."""
    columns = {c.name for c in AccountRecord.__table__.columns}
    forbidden = {"password", "passwd", "secret", "token", "cookie", "cookies",
                 "session_token", "auth_token", "otp"}
    assert not (columns & forbidden)


def test_a_verified_sign_in_is_recorded_and_resets_the_failure_count():
    authsess.record_attempt(DOMAIN, _result(AuthOutcome.FAILED, "wrong password"))
    authsess.record_attempt(DOMAIN, _result(AuthOutcome.AUTHENTICATED, "verified"))
    row = authsess.get_record(DOMAIN)
    assert row.last_verified_at is not None
    assert row.consecutive_failures == 0


def test_a_page_that_never_asked_is_not_recorded_as_verified():
    """Regression: NOT_REQUIRED would have set last_verified_at, making
    `session_is_fresh` claim a session for 12 hours on the strength of an
    error page that simply did not contain a login form."""
    authsess.record_attempt(DOMAIN, _result(AuthOutcome.NOT_REQUIRED, "not asking",
                                            state=AuthState.NOT_AUTH))
    row = authsess.get_record(DOMAIN)
    assert row.last_verified_at is None
    assert authsess.session_is_fresh(DOMAIN) is False


def test_an_indeterminate_result_is_not_recorded_as_verified():
    authsess.record_attempt(DOMAIN, _result(AuthOutcome.INDETERMINATE, "could not read"))
    assert authsess.get_record(DOMAIN).last_verified_at is None


def test_repeated_failures_stop_further_automatic_attempts():
    """A stored credential that is wrong stays wrong; hammering it is how
    "wrong password" becomes "account locked"."""
    for _ in range(authsess.MAX_CONSECUTIVE_FAILURES):
        authsess.record_attempt(DOMAIN, _result(AuthOutcome.FAILED, "wrong password"))
    allowed, why = authsess.should_attempt(DOMAIN)
    assert allowed is False
    assert "not retrying automatically" in why


def test_a_fresh_domain_is_always_attempted():
    allowed, _ = authsess.should_attempt("never-seen.example")
    assert allowed is True


def test_a_recorded_note_is_redacted():
    authsess.record_attempt(
        DOMAIN, _result(AuthOutcome.FAILED, "form said password=hunter2seekrit is wrong"))
    assert "hunter2seekrit" not in authsess.get_record(DOMAIN).note


def test_a_stale_verified_session_is_not_treated_as_fresh():
    authsess.record_attempt(DOMAIN, _result(AuthOutcome.AUTHENTICATED, "verified"))
    with session_scope() as s:
        row = s.execute(
            select(AccountRecord).where(AccountRecord.domain == DOMAIN)
        ).scalar_one()
        row.last_verified_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            hours=authsess.SESSION_TRUST_HOURS + 1)
        s.commit()
    assert authsess.session_is_fresh(DOMAIN) is False
