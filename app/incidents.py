"""Incident state machine: UP/DEGRADED/DOWN with hysteresis (RF-2/RF-3).

Rules:
- A check that connects and authenticates (UP or DEGRADED) is a *success* for
  incident purposes; only DOWN counts as failure.
- DOWN is confirmed after ``retries + 1`` consecutive failed checks, at the
  normal scheduled cadence (no immediate re-checks — courtesy first).
- The incident's ``started_at`` is the timestamp of the *first* failure of the
  streak, so the reported downtime covers the whole outage as observed.
- The first successful check closes the incident and records the duration.
- Events (opened/closed) are returned to the caller; the alerts module (Fase 4)
  consumes them.

The tracker reloads open incidents from the database on startup so an app
restart mid-outage neither loses nor duplicates the incident.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime

from app.db import Database
from app.errors import truncate
from app.models import CheckResult, ConnectionConfig, Status
from app.util import from_iso, to_iso, utc_now


@dataclass(frozen=True)
class IncidentOpened:
    incident_id: int
    connection_id: int
    started_at: datetime
    error_type: str | None
    message: str


@dataclass(frozen=True)
class IncidentClosed:
    incident_id: int
    connection_id: int
    started_at: datetime
    ended_at: datetime
    duration_s: float
    error_type: str | None


IncidentEvent = IncidentOpened | IncidentClosed


@dataclass
class _ConnState:
    consecutive_failures: int = 0
    failures_after_confirm: int = 0
    confirmed_down: bool = False
    first_failure_at: datetime | None = None
    first_error: tuple[str | None, str] = (None, "")
    open_incident_id: int | None = None
    incident_started_at: datetime | None = None
    stable_status: Status | None = None  # status with hysteresis applied
    last_raw_status: Status | None = None


class IncidentTracker:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._lock = threading.RLock()
        self._states: dict[int, _ConnState] = {}
        self._load_open_incidents()

    def _load_open_incidents(self) -> None:
        for row in self._db.list_open_incidents():
            state = _ConnState(
                consecutive_failures=1,
                failures_after_confirm=1,
                confirmed_down=True,
                open_incident_id=row["id"],
                incident_started_at=from_iso(row["started_at"]),
                first_failure_at=from_iso(row["started_at"]),
                first_error=(row["error_type"], row["first_error_msg"]),
                stable_status=Status.DOWN,
                last_raw_status=Status.DOWN,
            )
            self._states[row["connection_id"]] = state

    def record(
        self, cfg: ConnectionConfig, result: CheckResult, ts: datetime | None = None
    ) -> list[IncidentEvent]:
        """Persist one check result and advance the state machine."""
        if cfg.id is None:
            raise ValueError("connection must be persisted before recording checks")
        ts = ts or utc_now()
        with self._lock:
            self._db.insert_check(
                connection_id=cfg.id,
                ts_utc=to_iso(ts),
                status=result.status.value,
                latency_ms=result.latency_ms,
                error_type=result.error_type.value if result.error_type else None,
                error_msg=truncate(result.error_msg or ""),
            )
            state = self._states.setdefault(cfg.id, _ConnState())
            events: list[IncidentEvent] = []

            if result.status is Status.DOWN:
                state.consecutive_failures += 1
                if state.confirmed_down:
                    state.failures_after_confirm += 1
                if state.first_failure_at is None:
                    state.first_failure_at = ts
                    state.first_error = (
                        result.error_type.value if result.error_type else None,
                        truncate(result.error_msg or ""),
                    )
                needed = cfg.retries + 1
                if not state.confirmed_down and state.consecutive_failures >= needed:
                    state.confirmed_down = True
                    state.failures_after_confirm = 1
                    state.stable_status = Status.DOWN
                    state.incident_started_at = state.first_failure_at
                    incident_id = self._db.open_incident(
                        connection_id=cfg.id,
                        started_at=to_iso(state.first_failure_at),
                        error_type=state.first_error[0],
                        first_error_msg=state.first_error[1],
                    )
                    state.open_incident_id = incident_id
                    events.append(
                        IncidentOpened(
                            incident_id=incident_id,
                            connection_id=cfg.id,
                            started_at=state.first_failure_at,
                            error_type=state.first_error[0],
                            message=state.first_error[1],
                        )
                    )
            else:
                if state.confirmed_down and state.open_incident_id is not None:
                    started = state.incident_started_at or state.first_failure_at or ts
                    duration_s = max(0.0, (ts - started).total_seconds())
                    self._db.close_incident(state.open_incident_id, to_iso(ts), duration_s)
                    events.append(
                        IncidentClosed(
                            incident_id=state.open_incident_id,
                            connection_id=cfg.id,
                            started_at=started,
                            ended_at=ts,
                            duration_s=duration_s,
                            error_type=state.first_error[0],
                        )
                    )
                state.consecutive_failures = 0
                state.failures_after_confirm = 0
                state.confirmed_down = False
                state.first_failure_at = None
                state.first_error = (None, "")
                state.open_incident_id = None
                state.incident_started_at = None
                state.stable_status = result.status

            state.last_raw_status = result.status
            return events

    # --- read side (dashboard / scheduler) -----------------------------------

    def status_of(self, connection_id: int) -> Status | None:
        """Connection status with hysteresis: unconfirmed failures don't flip it."""
        with self._lock:
            state = self._states.get(connection_id)
            return state.stable_status if state else None

    def is_confirmed_down(self, connection_id: int) -> bool:
        with self._lock:
            state = self._states.get(connection_id)
            return bool(state and state.confirmed_down)

    def failures_since_confirm(self, connection_id: int) -> int:
        """Failed checks since DOWN was confirmed (drives the backoff exponent)."""
        with self._lock:
            state = self._states.get(connection_id)
            if not state or not state.confirmed_down:
                return 0
            return state.failures_after_confirm
