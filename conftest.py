"""Pytest root conftest: make the repository root importable (``import app``)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
