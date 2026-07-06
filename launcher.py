"""PyInstaller entry point (Modo A).

A tiny top-level script so PyInstaller has a static import graph rooted here.
Supports the same flags as ``python -m app.main`` (e.g. ``--demo``).
"""
import multiprocessing

from app.main import main

if __name__ == "__main__":
    # Safe under PyInstaller onedir if anything ever spawns a subprocess.
    multiprocessing.freeze_support()
    main()
