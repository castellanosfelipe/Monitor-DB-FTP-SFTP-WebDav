"""Pydantic schemas for the dashboard API.

Secrets never leave the server: responses expose only ``has_secret``. On
update, ``secret=None`` means "keep the stored one"; a string (also empty)
replaces it.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.models import DEFAULT_PORTS, ConnectionConfig, Protocol


class ConnectionIn(BaseModel):
    name: str
    client: str = ""
    protocol: Protocol
    host: str
    port: int | None = None
    username: str = ""
    secret: str | None = None
    auth_type: str = "password"
    key_path: str | None = None
    db_name: str | None = None
    ssl_mode: str = "preferred"
    targets: list[str] = Field(default_factory=list)
    health_query: str | None = None
    interval_s: int = 60
    timeout_s: float = 10.0
    retries: int = 2
    degraded_ms: int | None = None
    write_check: bool = False
    enabled: bool = True
    notes: str = ""

    def to_config(self, connection_id: int | None = None) -> ConnectionConfig:
        return ConnectionConfig(
            id=connection_id,
            name=self.name.strip(),
            client=self.client.strip(),
            protocol=self.protocol,
            host=self.host.strip(),
            port=self.port or DEFAULT_PORTS[self.protocol],
            username=self.username,
            auth_type=self.auth_type,
            key_path=(self.key_path or "").strip() or None,
            db_name=(self.db_name or "").strip() or None,
            ssl_mode=self.ssl_mode,
            targets=[t.strip() for t in self.targets if t.strip()],
            health_query=(self.health_query or "").strip() or None,
            interval_s=self.interval_s,
            timeout_s=self.timeout_s,
            retries=self.retries,
            degraded_ms=self.degraded_ms,
            write_check=self.write_check,
            enabled=self.enabled,
            notes=self.notes,
        )


class TestConnectionIn(ConnectionIn):
    # When testing an existing connection without retyping the password.
    id: int | None = None


class ConnectionOut(BaseModel):
    id: int
    name: str
    client: str
    protocol: str
    host: str
    port: int
    username: str
    has_secret: bool
    auth_type: str
    key_path: str | None
    db_name: str | None
    ssl_mode: str
    targets: list[str]
    health_query: str | None
    interval_s: int
    timeout_s: float
    retries: int
    degraded_ms: int | None
    write_check: bool
    enabled: bool
    notes: str
    created_at: str | None
    updated_at: str | None

    @classmethod
    def from_config(cls, cfg: ConnectionConfig) -> "ConnectionOut":
        return cls(
            id=cfg.id or 0,
            name=cfg.name,
            client=cfg.client,
            protocol=cfg.protocol.value,
            host=cfg.host,
            port=cfg.port,
            username=cfg.username,
            has_secret=bool(cfg.secret_encrypted),
            auth_type=cfg.auth_type,
            key_path=cfg.key_path,
            db_name=cfg.db_name,
            ssl_mode=cfg.ssl_mode,
            targets=cfg.targets,
            health_query=cfg.health_query,
            interval_s=cfg.interval_s,
            timeout_s=cfg.timeout_s,
            retries=cfg.retries,
            degraded_ms=cfg.degraded_ms,
            write_check=cfg.write_check,
            enabled=cfg.enabled,
            notes=cfg.notes,
            created_at=cfg.created_at,
            updated_at=cfg.updated_at,
        )


class TargetResultOut(BaseModel):
    target: str
    ok: bool
    error_type: str | None
    message: str


class CheckResultOut(BaseModel):
    status: str
    latency_ms: float | None
    error_type: str | None
    error_msg: str
    targets: list[TargetResultOut]
