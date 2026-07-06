"""Alert orchestration (RF-4): channels, anti-spam, reminders.

Anti-spam is structural: the incident state machine emits exactly one
``IncidentOpened`` and one ``IncidentClosed`` per outage, so each produces one
alert per channel. Optional reminders (off by default) re-alert every N
minutes while an incident stays open. Every attempt — success or failure —
lands in ``alerts_log``.
"""
from __future__ import annotations

import json
import logging
import smtplib
import threading
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from typing import Callable, Protocol as TypingProtocol

import httpx

from app import config
from app.db import Database
from app.incidents import IncidentClosed, IncidentEvent, IncidentOpened
from app.settings_store import get_int, get_str
from app.util import utc_now

logger = logging.getLogger(__name__)


def humanize_duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.1f} min"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} días"


@dataclass(frozen=True)
class Alert:
    severity: str  # "down" | "recovered" | "reminder"
    title: str
    message: str
    incident_id: int
    connection_id: int
    payload: dict  # structured version for the webhook


class Notifier(TypingProtocol):
    """Platform notification adapter (toast+tray on Windows, log elsewhere)."""

    channel: str

    def notify(self, alert: Alert) -> None: ...


class HeadlessNotifier:
    """Fallback (dev/CI, no-Windows): la alerta queda en logs, alerts_log y el banner."""

    channel = "log"

    def notify(self, alert: Alert) -> None:
        level = logging.ERROR if alert.severity != "recovered" else logging.INFO
        logger.log(level, "ALERTA [%s] %s — %s", alert.severity, alert.title, alert.message)


# --- optional channels -----------------------------------------------------------


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    starttls: bool
    username: str
    password: str
    mail_from: str
    mail_to: tuple[str, ...]


def smtp_config(db: Database) -> SmtpConfig | None:
    if get_str(db, "alerts.smtp_enabled") != "1":
        return None
    host = get_str(db, "smtp.host")
    to = tuple(x.strip() for x in get_str(db, "smtp.to").split(",") if x.strip())
    if not host or not to:
        return None
    password = get_str(db, "smtp.password")
    if password.startswith(("fernet:", "dpapi:")):  # stored via the secret store
        try:
            from app.platform.secretstore import get_secret_store

            password = get_secret_store().decrypt(password)
        except Exception:
            logger.warning("no se pudo descifrar smtp.password; se envía sin login")
            password = ""
    return SmtpConfig(
        host=host,
        port=get_int(db, "smtp.port") or 25,
        starttls=get_str(db, "smtp.starttls") == "1",
        username=get_str(db, "smtp.username"),
        password=password,
        mail_from=get_str(db, "smtp.from") or "monitor@localhost",
        mail_to=to,
    )


def send_smtp(cfg: SmtpConfig, alert: Alert, timeout: float = 15.0) -> None:
    msg = EmailMessage()
    msg["Subject"] = alert.title
    msg["From"] = cfg.mail_from
    msg["To"] = ", ".join(cfg.mail_to)
    msg.set_content(alert.message)
    with smtplib.SMTP(cfg.host, cfg.port, timeout=timeout) as smtp:
        if cfg.starttls:
            smtp.starttls()
        if cfg.username:
            smtp.login(cfg.username, cfg.password)
        smtp.send_message(msg)


def send_webhook(url: str, alert: Alert, timeout: float = 10.0) -> None:
    response = httpx.post(
        url,
        content=json.dumps(alert.payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "User-Agent": config.USER_AGENT},
        timeout=timeout,
    )
    response.raise_for_status()


# --- orchestrator ---------------------------------------------------------------


class Alerter:
    def __init__(
        self,
        db: Database,
        notifier: Notifier | None = None,
        clock: Callable[[], datetime] = utc_now,
        smtp_sender: Callable[[SmtpConfig, Alert], None] = send_smtp,
        webhook_sender: Callable[[str, Alert], None] = send_webhook,
    ) -> None:
        self._db = db
        self._notifier = notifier or HeadlessNotifier()
        self._clock = clock
        self._send_smtp = smtp_sender
        self._send_webhook = webhook_sender
        self._lock = threading.Lock()
        # incident_id -> (connection_id, last alert time); survives restarts
        # without re-alerting: on boot the "last" is set to now.
        self._open: dict[int, tuple[int, datetime]] = {}
        for row in self._db.list_open_incidents():
            self._open[row["id"]] = (row["connection_id"], self._clock())

    # --- event intake ------------------------------------------------------------

    def handle_events(self, events: list[IncidentEvent]) -> None:
        for event in events:
            if isinstance(event, IncidentOpened):
                self._on_opened(event)
            elif isinstance(event, IncidentClosed):
                self._on_closed(event)

    def _describe(self, connection_id: int) -> str:
        cfg = self._db.get_connection(connection_id)
        if cfg is None:
            return f"conexión #{connection_id}"
        client = f", cliente {cfg.client}" if cfg.client else ""
        return f"{cfg.name} ({cfg.protocol.value} {cfg.host}:{cfg.port}{client})"

    def _on_opened(self, event: IncidentOpened) -> None:
        description = self._describe(event.connection_id)
        cause = event.error_type or "desconocida"
        alert = Alert(
            severity="down",
            title=f"CAÍDA: {self._name(event.connection_id)}",
            message=f"{description} está CAÍDA. Causa: {cause} — {event.message}",
            incident_id=event.incident_id,
            connection_id=event.connection_id,
            payload={
                "event": "incident_opened",
                "incident_id": event.incident_id,
                "connection_id": event.connection_id,
                "connection": self._name(event.connection_id),
                "started_at": event.started_at.isoformat(),
                "error_type": event.error_type,
                "message": event.message,
            },
        )
        with self._lock:
            self._open[event.incident_id] = (event.connection_id, self._clock())
        self._dispatch(alert)

    def _on_closed(self, event: IncidentClosed) -> None:
        description = self._describe(event.connection_id)
        duration = humanize_duration(event.duration_s)
        alert = Alert(
            severity="recovered",
            title=f"RECUPERADO: {self._name(event.connection_id)}",
            message=f"{description} se RECUPERÓ tras {duration} de caída.",
            incident_id=event.incident_id,
            connection_id=event.connection_id,
            payload={
                "event": "incident_closed",
                "incident_id": event.incident_id,
                "connection_id": event.connection_id,
                "connection": self._name(event.connection_id),
                "started_at": event.started_at.isoformat(),
                "ended_at": event.ended_at.isoformat(),
                "duration_s": event.duration_s,
                "error_type": event.error_type,
            },
        )
        with self._lock:
            self._open.pop(event.incident_id, None)
        self._dispatch(alert)

    def _name(self, connection_id: int) -> str:
        cfg = self._db.get_connection(connection_id)
        return cfg.name if cfg is not None else f"conexión #{connection_id}"

    # --- reminders (off by default) --------------------------------------------------

    def check_reminders(self) -> None:
        """Called periodically by the scheduler; re-alerts long-running incidents."""
        minutes = get_int(self._db, "alerts.reminder_minutes")
        if minutes <= 0:
            return
        now = self._clock()
        due: list[tuple[int, int]] = []
        with self._lock:
            for incident_id, (connection_id, last) in self._open.items():
                if (now - last).total_seconds() >= minutes * 60:
                    self._open[incident_id] = (connection_id, now)
                    due.append((incident_id, connection_id))
        for incident_id, connection_id in due:
            description = self._describe(connection_id)
            alert = Alert(
                severity="reminder",
                title=f"SIGUE CAÍDA: {self._name(connection_id)}",
                message=f"{description} sigue caída (recordatorio cada {minutes} min).",
                incident_id=incident_id,
                connection_id=connection_id,
                payload={
                    "event": "incident_reminder",
                    "incident_id": incident_id,
                    "connection_id": connection_id,
                    "connection": self._name(connection_id),
                },
            )
            self._dispatch(alert)

    # --- channel dispatch ------------------------------------------------------------

    def _dispatch(self, alert: Alert) -> None:
        try:
            self._notifier.notify(alert)
            self._db.log_alert(alert.incident_id, self._notifier.channel, ok=True)
        except Exception as exc:
            logger.exception("fallo notificando por %s", self._notifier.channel)
            self._db.log_alert(alert.incident_id, self._notifier.channel, ok=False, detail=str(exc))

        smtp = smtp_config(self._db)
        if smtp is not None:
            try:
                self._send_smtp(smtp, alert)
                self._db.log_alert(alert.incident_id, "smtp", ok=True)
            except Exception as exc:
                logger.warning("fallo enviando alerta SMTP: %s", exc)
                self._db.log_alert(alert.incident_id, "smtp", ok=False, detail=str(exc))

        if get_str(self._db, "alerts.webhook_enabled") == "1":
            url = get_str(self._db, "webhook.url")
            if url:
                try:
                    self._send_webhook(url, alert)
                    self._db.log_alert(alert.incident_id, "webhook", ok=True)
                except Exception as exc:
                    logger.warning("fallo enviando webhook: %s", exc)
                    self._db.log_alert(alert.incident_id, "webhook", ok=False, detail=str(exc))
