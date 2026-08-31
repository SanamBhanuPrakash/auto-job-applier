"""Preflight checks. The point is that they report honestly."""
from __future__ import annotations

from jobbot.preflight import CHECKS, Status, ready, run_preflight


def test_every_check_returns_a_result_even_when_the_environment_is_bare():
    results = run_preflight()
    assert len(results) == len(CHECKS)
    assert all(r.name and r.detail for r in results)


def test_a_failing_check_carries_a_fix_the_user_can_act_on():
    for check in run_preflight():
        if check.status is Status.FAIL:
            assert check.fix, f"{check.name} failed with no suggested fix"


def test_readiness_is_false_when_anything_failed():
    results = run_preflight()
    assert ready(results) is (not any(c.status is Status.FAIL for c in results))


def test_a_check_that_raises_becomes_a_warning_not_a_crash(monkeypatch):
    """`jobbot doctor` exists to tell you what is wrong. A doctor that
    dies on the way is worse than no doctor."""
    import jobbot.preflight as pf

    def exploding():
        raise RuntimeError("boom")

    monkeypatch.setattr(pf, "CHECKS", (exploding,))
    results = pf.run_preflight()
    assert len(results) == 1
    assert results[0].status is Status.WARN
    assert "boom" in results[0].detail


def test_auto_submit_is_reported_as_a_warning_when_on(monkeypatch):
    """The setting that sends applications with nobody watching must be
    visible before a batch, not discovered after one."""
    from jobbot import config

    settings = config.get_settings()
    monkeypatch.setattr(settings, "jobbot_auto_submit", True)
    check = next(c for c in run_preflight() if c.name == "submission mode")
    assert check.status is Status.WARN
    assert "AUTO-SUBMIT IS ON" in check.detail


def test_review_mode_is_the_reported_default(monkeypatch):
    from jobbot import config

    monkeypatch.setattr(config.get_settings(), "jobbot_auto_submit", False)
    check = next(c for c in run_preflight() if c.name == "submission mode")
    assert check.status is Status.OK
    assert "waits for you" in check.detail


def test_profile_check_passes_for_a_correctly_filled_in_profile(monkeypatch):
    """Regression test: the Profile schema (jobbot/resume/schema.py) and
    every profile.yaml this project generates call the field "name", not
    "full_name" — checking the wrong key here meant this FAILed even for a
    profile.yaml that matched config/profile.example.yaml exactly."""
    from jobbot import config as config_module
    from jobbot import preflight

    # _check_profile() does `from jobbot.config import load_profile_raw`
    # inside the function body, so patching the source attribute is what
    # a fresh call actually picks up.
    monkeypatch.setattr(config_module, "load_profile_raw", lambda: {"name": "Ada Lovelace", "email": "ada@example.com"})

    check = preflight._check_profile()

    assert check.status is Status.OK
    assert "Ada Lovelace" in check.detail
    assert "ada@example.com" in check.detail
