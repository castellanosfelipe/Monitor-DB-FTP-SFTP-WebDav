"""Application logging (RF-3): rotating file (logs/app.log)."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from app import config

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(mode: str) -> None:
    """Rotating file log (RF-3). ``mode`` is accepted for signature stability."""
    root = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return  # already configured (tests, reloads)
    root.setLevel(logging.INFO)

    file_handler = RotatingFileHandler(
        config.logs_dir() / "app.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(file_handler)
