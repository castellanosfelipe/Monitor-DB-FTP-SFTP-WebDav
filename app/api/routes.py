"""Dashboard REST routes: CRUD, test-connection, overview, pause/resume."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from app.api.schemas import (
    CheckResultOut,
    ConnectionIn,
    ConnectionOut,
    TargetResultOut,
    TestConnectionIn,
)
from app.models import CheckResult, ConnectionConfig, validate_connection
from app.platform.secretstore import SecretStoreError
from app.util import to_iso, utc_now

router = APIRouter(prefix="/api")


def _ctx(request: Request) -> Any:
    return request.app.state.ctx


def _get_or_404(ctx: Any, connection_id: int) -> ConnectionConfig:
    cfg = ctx.db.get_connection(connection_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail="La conexión no existe.")
    return cfg


def _validate_or_422(cfg: ConnectionConfig) -> None:
    problems = validate_connection(cfg)
    if problems:
        raise HTTPException(status_code=422, detail=problems)


def _encrypt_secret(ctx: Any, plain: str | None) -> str | None:
    if not plain:
        return None
    if ctx.secret_store is None:
        raise HTTPException(status_code=500, detail="No hay almacén de secretos configurado.")
    return ctx.secret_store.encrypt(plain)


def _result_out(result: CheckResult) -> CheckResultOut:
    return CheckResultOut(
        status=result.status.value,
        latency_ms=result.latency_ms,
        error_type=result.error_type.value if result.error_type else None,
        error_msg=result.error_msg,
        targets=[
            TargetResultOut(
                target=t.target,
                ok=t.ok,
                error_type=t.error_type.value if t.error_type else None,
                message=t.message,
            )
            for t in result.targets
        ],
    )


# --- CRUD -----------------------------------------------------------------------


@router.get("/connections")
def list_connections(request: Request) -> list[ConnectionOut]:
    ctx = _ctx(request)
    return [ConnectionOut.from_config(c) for c in ctx.db.list_connections()]


@router.get("/connections/{connection_id}")
def get_connection(request: Request, connection_id: int) -> ConnectionOut:
    return ConnectionOut.from_config(_get_or_404(_ctx(request), connection_id))


@router.post("/connections", status_code=201)
def create_connection(request: Request, payload: ConnectionIn) -> ConnectionOut:
    ctx = _ctx(request)
    cfg = payload.to_config()
    _validate_or_422(cfg)
    cfg.secret_encrypted = _encrypt_secret(ctx, payload.secret)
    ctx.db.create_connection(cfg)
    if ctx.engine is not None:
        ctx.engine.schedule_connection(cfg.id, immediate=True)
    return ConnectionOut.from_config(cfg)


@router.put("/connections/{connection_id}")
def update_connection(request: Request, connection_id: int, payload: ConnectionIn) -> ConnectionOut:
    ctx = _ctx(request)
    existing = _get_or_404(ctx, connection_id)
    cfg = payload.to_config(connection_id)
    _validate_or_422(cfg)
    if payload.secret is None:
        cfg.secret_encrypted = existing.secret_encrypted  # keep the stored secret
    else:
        cfg.secret_encrypted = _encrypt_secret(ctx, payload.secret)
    ctx.db.update_connection(cfg)
    if ctx.engine is not None:
        ctx.engine.schedule_connection(connection_id)
    return ConnectionOut.from_config(cfg)


@router.delete("/connections/{connection_id}")
def delete_connection(request: Request, connection_id: int) -> Response:
    ctx = _ctx(request)
    _get_or_404(ctx, connection_id)
    ctx.db.delete_connection(connection_id)
    if ctx.engine is not None:
        ctx.engine.unschedule_connection(connection_id)
    return Response(status_code=204)


@router.post("/connections/{connection_id}/duplicate", status_code=201)
def duplicate_connection(request: Request, connection_id: int) -> ConnectionOut:
    ctx = _ctx(request)
    cfg = _get_or_404(ctx, connection_id)
    cfg.id = None
    cfg.name = f"{cfg.name} (copia)"
    cfg.enabled = False  # the copy starts paused: same host, courtesy first
    ctx.db.create_connection(cfg)
    return ConnectionOut.from_config(cfg)


@router.post("/connections/{connection_id}/toggle")
def toggle_connection(request: Request, connection_id: int) -> ConnectionOut:
    ctx = _ctx(request)
    cfg = _get_or_404(ctx, connection_id)
    cfg.enabled = not cfg.enabled
    ctx.db.update_connection(cfg)
    if ctx.engine is not None:
        if cfg.enabled:
            ctx.engine.schedule_connection(connection_id, immediate=True)
        else:
            ctx.engine.unschedule_connection(connection_id)
    return ConnectionOut.from_config(cfg)


# --- probar conexión ---------------------------------------------------------------


@router.post("/connections/test")
def test_connection(request: Request, payload: TestConnectionIn) -> CheckResultOut:
    """Run one full check from the form, before saving (RF-1).

    Runs through the same courtesy throttle as scheduled checks. If ``id`` is
    given and no secret typed, the stored secret is reused.
    """
    ctx = _ctx(request)
    cfg = payload.to_config(payload.id)
    _validate_or_422(cfg)
    secret = payload.secret
    if secret is None and payload.id is not None:
        stored = ctx.db.get_connection(payload.id)
        if stored is not None and stored.secret_encrypted:
            try:
                secret = ctx.secret_store.decrypt(stored.secret_encrypted)
            except SecretStoreError as exc:
                raise HTTPException(status_code=409, detail=str(exc))
    from app.checkers import get_checker  # local import to keep module load light

    checker = get_checker(cfg.protocol)
    with ctx.throttle.slot(cfg.host):
        result = checker.check(cfg, secret)
    return _result_out(result)


# --- estado en vivo -------------------------------------------------------------------


@router.get("/overview")
def overview(request: Request) -> dict[str, Any]:
    ctx = _ctx(request)
    now = utc_now()
    windows = {
        "h24": to_iso(now - timedelta(hours=24)),
        "d7": to_iso(now - timedelta(days=7)),
        "d30": to_iso(now - timedelta(days=30)),
    }
    uptime = {key: ctx.db.uptime_counts(since) for key, since in windows.items()}
    avg_latency = ctx.db.avg_latencies(windows["h24"])
    latest = ctx.db.latest_checks()
    open_incidents = {r["connection_id"]: r for r in ctx.db.list_open_incidents()}

    cards = []
    clients: set[str] = set()
    for cfg in ctx.db.list_connections():
        cid = cfg.id or 0
        clients.add(cfg.client)
        last = latest.get(cid)
        live_status = ctx.tracker.status_of(cid)
        status = live_status.value if live_status else (last["status"] if last else None)
        if not cfg.enabled:
            status = "PAUSED"

        def pct(window: str) -> float | None:
            counts = uptime[window].get(cid)
            if not counts or counts[1] == 0:
                return None
            return round(100.0 * counts[0] / counts[1], 2)

        incident = open_incidents.get(cid)
        cards.append(
            {
                "id": cid,
                "name": cfg.name,
                "client": cfg.client,
                "protocol": cfg.protocol.value,
                "host": cfg.host,
                "port": cfg.port,
                "enabled": cfg.enabled,
                "status": status,
                "interval_s": cfg.interval_s,
                "last_check_ts": last["ts_utc"] if last else None,
                "last_latency_ms": last["latency_ms"] if last else None,
                "avg_latency_ms": avg_latency.get(cid),
                "last_error_type": last["error_type"] if last else None,
                "last_error_msg": last["error_msg"] if last else None,
                "uptime": {"h24": pct("h24"), "d7": pct("d7"), "d30": pct("d30")},
                "open_incident": (
                    {
                        "id": incident["id"],
                        "started_at": incident["started_at"],
                        "error_type": incident["error_type"],
                        "message": incident["first_error_msg"],
                    }
                    if incident
                    else None
                ),
            }
        )

    return {
        "generated_at": to_iso(now),
        "paused": bool(ctx.engine is not None and ctx.engine.paused),
        "clients": sorted(c for c in clients if c),
        "connections": cards,
    }


@router.get("/connections/{connection_id}/history")
def connection_history(request: Request, connection_id: int, hours: int = 24) -> dict[str, Any]:
    ctx = _ctx(request)
    _get_or_404(ctx, connection_id)
    hours = max(1, min(hours, 24 * 31))
    since = to_iso(utc_now() - timedelta(hours=hours))
    checks = [
        {
            "ts_utc": r["ts_utc"],
            "status": r["status"],
            "latency_ms": r["latency_ms"],
            "error_type": r["error_type"],
            "error_msg": r["error_msg"],
        }
        for r in ctx.db.list_checks(connection_id, since)
    ]
    incidents = [
        {
            "id": r["id"],
            "started_at": r["started_at"],
            "ended_at": r["ended_at"],
            "duration_s": r["duration_s"],
            "error_type": r["error_type"],
            "first_error_msg": r["first_error_msg"],
        }
        for r in ctx.db.list_incidents(connection_id)
    ]
    return {"checks": checks, "incidents": incidents}


# --- pausa global ------------------------------------------------------------------------


@router.post("/pause")
def pause_all(request: Request) -> dict[str, bool]:
    ctx = _ctx(request)
    if ctx.engine is not None:
        ctx.engine.pause_all()
    return {"paused": True}


@router.post("/resume")
def resume_all(request: Request) -> dict[str, bool]:
    ctx = _ctx(request)
    if ctx.engine is not None:
        ctx.engine.resume_all()
    return {"paused": False}
