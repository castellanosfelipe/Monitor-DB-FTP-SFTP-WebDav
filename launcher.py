"""PyInstaller entry point (Modo A).

A tiny top-level script so PyInstaller has a static import graph rooted here.
Supports the same flags as ``python -m app.main`` (e.g. ``--demo``).
"""
from __future__ import annotations

import multiprocessing
import os
import sys
from typing import TextIO

_NULL_INPUT: TextIO | None = None
_NULL_OUTPUT: TextIO | None = None


def _ensure_standard_streams() -> None:
    """Provide stdio handles when PyInstaller runs as ``--noconsole``."""
    global _NULL_INPUT, _NULL_OUTPUT
    if sys.stdin is None:
        _NULL_INPUT = open(os.devnull, "r", encoding="utf-8")
        sys.stdin = _NULL_INPUT
    if sys.stdout is None or sys.stderr is None:
        if _NULL_OUTPUT is None or _NULL_OUTPUT.closed:
            _NULL_OUTPUT = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = _NULL_OUTPUT
        if sys.stderr is None:
            sys.stderr = _NULL_OUTPUT


_ensure_standard_streams()

from app.main import main

if __name__ == "__main__":
    # Safe under PyInstaller onedir if anything ever spawns a subprocess.
    multiprocessing.freeze_support()
    main()
