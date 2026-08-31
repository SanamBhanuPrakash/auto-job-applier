import logging
import sys

from jobbot.config import get_settings


def _make_console_unicode_safe() -> None:
    """Windows' default console codepage (cp1252 etc.) can't encode a lot of
    ordinary punctuation that shows up in scraped job postings and resumes
    — en/em dashes, smart quotes, non-breaking hyphens. Confirmed live:
    logging one such line crashed the log call with UnicodeEncodeError from
    inside logging's own error handler, which dumps a multi-page traceback
    to the console every time it happens (not fatal to the process — the
    caller isn't touched — but noisy enough to bury the actual output, and
    a real crash risk anywhere else something writes non-ASCII straight to
    stdout/stderr without logging's built-in error recovery). Reconfiguring
    both streams to replace unencodable characters instead of raising is
    the standard fix; only meaningful on platforms where reconfigure exists
    and the stream isn't already UTF-8.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="backslashreplace")
        except Exception:  # noqa: BLE001
            pass  # best-effort; worst case the original crash risk remains


def setup_logging(verbose: bool = False) -> None:
    _make_console_unicode_safe()
    settings = get_settings()
    log_path = settings.data_dir / "jobbot.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
