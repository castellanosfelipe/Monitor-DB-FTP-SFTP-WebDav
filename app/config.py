"""Application paths, runtime constants and directory resolution.

All paths derive from a single base directory so the app is fully portable:
next to the executable when frozen with PyInstaller (Modo A), the repository
root during development, or wherever ``MONITOR_DATA_DIR`` points (Modo B mounts
a volume there).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from app import __version__

APP_NAME = "StabilityMonitor"
USER_AGENT = f"{APP_NAME}/{__version__}"
DEFAULT_PORT = 8090


def base_dir() -> Path:
    env = os.environ.get("MONITOR_DATA_DIR", "").strip()
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):  # PyInstaller onedir bundle
        return Path(sys.executable).resolve().parent
    if os.environ.get("VERCEL"):
        # Serverless: el bundle es de solo lectura; lo efímero va a /tmp
        # (la persistencia real vive en Postgres/Neon).
        return Path("/tmp/stability-monitor")
    return Path(__file__).resolve().parent.parent


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_dir() -> Path:
    return _ensure(base_dir() / "data")


def logs_dir() -> Path:
    return _ensure(base_dir() / "logs")


def reports_dir() -> Path:
    return _ensure(base_dir() / "reports")


def db_path() -> Path:
    return data_dir() / "monitor.db"


def known_hosts_path() -> Path:
    """SSH known_hosts file used by the SFTP checker (TOFU policy)."""
    return data_dir() / "known_hosts"
