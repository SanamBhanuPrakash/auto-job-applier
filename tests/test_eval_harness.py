"""The fault-injection suite, run as part of the normal test run.

Kept in pytest deliberately. An evaluation harness that only runs when
someone remembers to run it measures nothing: this is the suite that found
the false-confirmation defect (failures §17), and it found it on the first
execution.

It is offline — local fixture pages, no LLM calls, no job boards — so it
costs a few seconds and can run on every change.
"""
from __future__ import annotations

import os

import pytest
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from jobbot.eval.metrics import CRITICAL_METRICS
from jobbot.eval.runner import EvalRunner
from jobbot.eval.scenarios import SCENARIOS, Expect

_CHROME = os.environ.get("JOBBOT_TEST_CHROMIUM_PATH") or None


@pytest.fixture(scope="module")
def report():
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True, executable_path=_CHROME)
        except PlaywrightError as exc:
            pytest.skip(f"Chromium not installed: {exc}")
        try:
            yield EvalRunner(browser).run_all()
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass


# --- the suite itself is well-formed --------------------------------------


def test_every_scenario_the_spec_names_is_declared():
    assert len(SCENARIOS) == 48
    assert len({s.name for s in SCENARIOS}) == 48


def test_unbuilt_capabilities_are_declared_rather_than_omitted():
    """A harness listing only what already works reports a clean sweep and
    tells you nothing. Every unbuilt scenario names what is missing."""
    unbuilt = [s for s in SCENARIOS if not s.implemented]
    assert unbuilt, "at least the auth scenarios are known to be unbuilt"
    for s in unbuilt:
        assert s.requires and "not built" in s.requires or "not exposed" in s.requires


def test_most_scenarios_expect_a_refusal_rather_than_a_submission():
    """Sanity check on the suite's own framing: this is a safety harness,
    so a scenario that passes by submitting should be rare."""
    submitting = [s for s in SCENARIOS if s.expect is Expect.SUBMITS]
    assert len(submitting) <= 2


# --- the results -----------------------------------------------------------


@pytest.mark.parametrize("metric", CRITICAL_METRICS)
def test_critical_metrics_are_zero(report, metric):
    """§93. These outrank every capability metric: a system that applies to
    more jobs while occasionally submitting twice, or answering a legal
    attestation on the candidate's behalf, is worse than one that applies
    to fewer."""
    value = getattr(report.metrics, metric)
    assert value == 0.0, f"{metric} = {value}; {report.gate_summary()}"


def test_no_scenario_fails(report):
    failed = [(r.name, r.reason) for r in report.results if not r.passed and not r.skipped]
    assert not failed, failed


def test_skips_are_only_for_capabilities_that_do_not_exist(report):
    """A skip must mean "not built", never "did not get around to running".
    Anything else would let a real regression hide as a skip."""
    for result in report.results:
        if result.skipped:
            assert "not built" in result.reason or "not exposed" in result.reason


def test_the_suite_actually_exercised_the_browser(report):
    """Guards against the whole thing silently no-opping."""
    assert report.metrics.scenarios_passed >= 30
    assert report.metrics.browser_seconds > 0


def test_an_unmeasured_critical_metric_does_not_count_as_clean():
    """A rate of "0 out of nothing" reads as a pass and is the same
    false-success shape this project treats as a defect (failures §14)."""
    from jobbot.eval.metrics import EvalReport, Metrics

    empty = EvalReport(metrics=Metrics())
    assert empty.metrics.false_submission_rate is None
    assert empty.critical_clean is False
    assert empty.gate_summary()["unmeasured_critical_metrics"]
