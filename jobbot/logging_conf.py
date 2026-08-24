import logging
import sys

from jobbot.config import get_settings


def setup_logging(verbose: bool = False) -> None:
    settings = get_settings()
    log_path = settings.data_dir / "jobbot.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
