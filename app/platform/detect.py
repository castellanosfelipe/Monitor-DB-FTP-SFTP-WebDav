"""Runtime mode detection.

Modes:
- ``windows``: Modo A (portable, DPAPI, tray/toasts).
- ``docker``:  Modo B (headless, Fernet with mandatory ``MONITOR_SECRET_KEY``).
- ``dev``:     implicit fallback for development on non-Windows machines outside
  a container; behaves like ``docker`` except the Fernet key may live in a
  local keyfile. Not forceable via env (per spec only windows|docker are).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def runtime_mode() -> str:
    forced = os.environ.get("MONITOR_MODE", "").strip().lower()
    if forced in ("windows", "docker"):
        return forced
    if sys.platform == "win32":
        return "windows"
    if Path("/.dockerenv").exists():
        return "docker"
    return "dev"
