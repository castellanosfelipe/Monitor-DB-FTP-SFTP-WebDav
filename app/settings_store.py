"""Typed access to the ``settings`` table with sane defaults (RF-7).

Settings are stored as strings; this module is the single place that knows
each key's type and default. The courtesy parameters feed the Throttle and
the scheduler.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.db import Database
from app.throttle import CourtesyPolicy

# Defaults (spec RF-2 / RF-3 / RF-7)
DEFAULTS: dict[str, str] = {
    "courtesy.global_concurrency": "10",
    "courtesy.host_spacing_s": "5",
    "courtesy.host_max_checks_per_min": "6",
    "courtesy.backoff_cap_s": "300",
    "courtesy.jitter_ratio": "0.10",
    "retention.days": "365",
    "alerts.reminder_minutes": "0",  # 0 = sin recordatorios
    "branding.company": "",
    "branding.accent": "#2563eb",
    "branding.logo_b64": "",
    "ui.language": "es",
}


def get_str(db: Database, key: str) -> str:
    value = db.get_setting(key)
    if value is None:
        return DEFAULTS.get(key, "")
    return value


def get_int(db: Database, key: str) -> int:
    try:
        return int(float(get_str(db, key)))
    except ValueError:
        return int(float(DEFAULTS[key]))


def get_float(db: Database, key: str) -> float:
    try:
        return float(get_str(db, key))
    except ValueError:
        return float(DEFAULTS[key])


def courtesy_policy(db: Database) -> CourtesyPolicy:
    return CourtesyPolicy(
        global_concurrency=max(1, get_int(db, "courtesy.global_concurrency")),
        host_spacing_s=max(0.0, get_float(db, "courtesy.host_spacing_s")),
        host_max_checks_per_min=max(1, get_int(db, "courtesy.host_max_checks_per_min")),
        backoff_cap_s=max(30.0, get_float(db, "courtesy.backoff_cap_s")),
        jitter_ratio=min(0.5, max(0.0, get_float(db, "courtesy.jitter_ratio"))),
    )


@dataclass(frozen=True)
class DashboardAuth:
    """HTTP Basic credentials for the dashboard; ``enabled=False`` only in Modo A."""

    enabled: bool
    username: str = ""
    password: str = ""
