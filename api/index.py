"""Vercel entry point: exposes the FastAPI app as a serverless function.

Storage lives in Neon (``DATABASE_URL``); checks run via ``/api/cron/tick``.
"""
from app.logging_setup import setup_logging
from app.main import create_app
from app.platform.detect import runtime_mode

setup_logging(runtime_mode())
app = create_app()
