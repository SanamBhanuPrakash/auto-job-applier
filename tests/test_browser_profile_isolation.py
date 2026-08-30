"""Per-worker browser profile isolation (spec §84, Phase 10).

Chromium takes a SingletonLock on a user-data directory. Two concurrent
workers pointed at the same one either fail to launch or silently attach
to the same browser session — sharing cookies, and with them the logged-in
identity and any rate-limit state the site is tracking.
"""
from __future__ import annotations

import pytest

from jobbot.submit.base import _safe_worker, _user_data_dir


def test_each_worker_gets_its_own_profile_directory():
    a = _user_data_dir("worker-1")
    b = _user_data_dir("worker-2")
    assert a != b
    assert a.exists() and b.exists()


def test_the_default_worker_keeps_the_original_path():
    """Zero-regression: an existing install has a manual login saved in
    `browser_profile`, and must not silently start from a fresh one."""
    assert _user_data_dir().name == "browser_profile"
    assert _user_data_dir("").name == "browser_profile"


def test_the_same_worker_id_is_stable_across_calls():
    assert _user_data_dir("w1") == _user_data_dir("w1")


@pytest.mark.parametrize("hostile,expected_not_in", [
    ("../../etc", ".."),
    ("a/b/c", "/"),
    ("..\\..\\windows", "\\"),
    ("worker\x00null", "\x00"),
])
def test_a_worker_id_cannot_escape_the_data_directory(hostile, expected_not_in):
    """Worker ids reach the filesystem. They are constrained, not trusted."""
    name = _safe_worker(hostile)
    assert expected_not_in not in name
    path = _user_data_dir(hostile)
    assert path.parent == _user_data_dir().parent


def test_an_empty_or_all_junk_worker_id_still_yields_a_usable_name():
    assert _safe_worker("///") == "worker"
    assert _safe_worker("") == "worker"


def test_a_very_long_worker_id_is_truncated():
    assert len(_safe_worker("w" * 500)) <= 40
