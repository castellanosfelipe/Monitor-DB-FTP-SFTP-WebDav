"""Application logging (RF-3): rotating file + stdout in docker mode."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from app import config

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(mode: str) -> None:
    root = logging.getLogger()
    formatter = logging.Formatter(_FORMAT)

    if mode == "serverless":
        # Vercel captura stdout; el disco es efímero, sin archivo rotativo.
        if not root.handlers:
            stream = logging.StreamHandler(sys.stdout)
            stream.setFormatter(formatter)
            root.addHandler(stream)
            root.setLevel(logging.INFO)
        return

    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return  # already configured (tests, reloads)
    root.setLevel(logging.INFO)

    file_handler = RotatingFileHandler(
        config.logs_dir() / "app.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if mode == "docker":
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        root.addHandler(stream)
