"""Backoff for flaky HTTP APIs and human-like pacing for browser submissions."""
from __future__ import annotations

import logging
import random
import time

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException))


http_retry = retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=20),
    retry=retry_if_exception(_is_retryable),
)


def human_pause(min_seconds: float, max_seconds: float) -> None:
    """Sleep a randomized interval so batch submissions don't look scripted."""
    delay = random.uniform(min_seconds, max_seconds)
    log.info("Pacing: sleeping %.1fs before next action", delay)
    time.sleep(delay)
