"""`jobbot run` chains discover -> match -> apply. Real failure hit live:
every configured LLM provider's free-tier daily quota ran out during the
match phase, and the resulting DailyQuotaExceeded crashed the whole
command — including the apply phase, which needs no LLM calls for jobs
that were already scored on an earlier day. Losing today's *new* scoring
must not also block reviewing/applying to whatever already cleared the bar.
"""
from __future__ import annotations

import jobbot.cli as cli
from jobbot.llm import DailyQuotaExceeded


def test_run_still_applies_when_matching_hits_a_daily_quota(monkeypatch):
    monkeypatch.setattr(cli, "_do_discover", lambda days: (0, 0))

    def exploding_match(top_n):
        raise DailyQuotaExceeded("Hit a daily token/request quota on this model's free tier")

    monkeypatch.setattr(cli, "_do_match", exploding_match)

    batch_calls = []
    monkeypatch.setattr(
        cli, "batch",
        lambda **kwargs: batch_calls.append(kwargs),
    )

    cli.run(min_score=75, limit=10, top_n=50, days=None, auto_submit=False, autofill_sensitive=None)

    assert len(batch_calls) == 1, "apply phase must still run after a daily-quota match failure"
    assert batch_calls[0]["min_score"] == 75
    assert batch_calls[0]["limit"] == 10


def test_run_still_applies_normally_when_matching_succeeds(monkeypatch):
    """Sanity check the fix doesn't change the ordinary, no-error path."""
    monkeypatch.setattr(cli, "_do_discover", lambda days: (3, 1))
    monkeypatch.setattr(cli, "_do_match", lambda top_n: 5)

    batch_calls = []
    monkeypatch.setattr(cli, "batch", lambda **kwargs: batch_calls.append(kwargs))

    cli.run(min_score=75, limit=10, top_n=50, days=None, auto_submit=False, autofill_sensitive=None)

    assert len(batch_calls) == 1


def test_run_lets_other_exceptions_propagate(monkeypatch):
    """Only the specific, well-understood daily-quota case should be
    swallowed — an unrelated crash in matching must still surface, not be
    silently papered over."""
    import pytest

    monkeypatch.setattr(cli, "_do_discover", lambda days: (0, 0))

    def exploding_match(top_n):
        raise ValueError("something genuinely unexpected")

    monkeypatch.setattr(cli, "_do_match", exploding_match)
    monkeypatch.setattr(cli, "batch", lambda **kwargs: pytest.fail("apply must not run after an unrelated crash"))

    with pytest.raises(ValueError, match="something genuinely unexpected"):
        cli.run(min_score=75, limit=10, top_n=50, days=None, auto_submit=False, autofill_sensitive=None)
