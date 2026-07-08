"""Dashboard REST routes: CRUD, test-connection, overview, pause/resume."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from app.api.schemas import (
    CheckResultOut,
    ConnectionIn,
    ConnectionOut,
    TargetResultOut,
    TestConnectionIn,
)
from app.models import (
    CheckResult,
    ConnectionAlias,
    ConnectionConfig,
    alias_key,
    parse_aliases_json,
    validate_connection,
)
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


def _import_plain_secret(item: dict[str, Any]) -> str | None:
    """Plaintext secret accepted only on import; normal exports never include it."""
    for key in ("secret", "password", "contrasena", "contraseña", "plain_secret"):
        value = item.get(key)
        if value is not None and str(value) != "":
            return str(value)
    return None


def _merge_alias_metadata(
    aliases: list[ConnectionAlias], existing: list[ConnectionAlias] | None = None
) -> list[ConnectionAlias]:
    """Keep per-alias timestamps while treating aliases as local metadata."""
    now = to_iso(utc_now())
    previous = {alias_key(a.name): a for a in (existing or [])}
    merged: list[ConnectionAlias] = []
    for alias in aliases:
        key = alias_key(alias.name)
        old = previous.get(key)
        created_at = old.created_at if old and old.created_at else now
        changed = old is None or old.name != alias.name or old.enabled != alias.enabled
        merged.append(
            ConnectionAlias(
                name=alias.name,
                enabled=alias.enabled,
                created_at=created_at,
                updated_at=now if changed else old.updated_at,
            )
        )
    return merged


def _monitoring_params_changed(before: ConnectionConfig, after: ConnectionConfig) -> bool:
    """True only when a change can affect real network/database checks."""
    return (
        before.protocol,
        before.host,
        before.port,
        before.username,
        before.secret_encrypted,
        before.auth_type,
        before.key_path,
        before.db_name,
        before.sql_instance,
        before.ssl_mode,
        tuple(before.targets),
        before.health_query,
        before.interval_s,
        before.timeout_s,
        before.retries,
        before.degraded_ms,
        before.write_check,
        before.enabled,
    ) != (
        after.protocol,
        after.host,
        after.port,
        after.username,
        after.secret_encrypted,
        after.auth_type,
        after.key_path,
        after.db_name,
        after.sql_instance,
        after.ssl_mode,
        tuple(after.targets),
        after.health_query,
        after.interval_s,
        after.timeout_s,
        after.retries,
        after.degraded_ms,
        after.write_check,
        after.enabled,
    )


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
    cfg.aliases = _merge_alias_metadata(cfg.aliases)
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
    cfg.aliases = _merge_alias_metadata(cfg.aliases, existing.aliases)
    _validate_or_422(cfg)
    if payload.secret is None:
        cfg.secret_encrypted = existing.secret_encrypted  # keep the stored secret
    else:
        cfg.secret_encrypted = _encrypt_secret(ctx, payload.secret)
    ctx.db.update_connection(cfg)
    if ctx.engine is not None:
        if not cfg.enabled:
            ctx.engine.unschedule_connection(connection_id)
        elif not existing.enabled:
            ctx.engine.schedule_connection(connection_id, immediate=True)
        elif _monitoring_params_changed(existing, cfg):
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
    cfg.aliases = _merge_alias_metadata(cfg.aliases)
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
                "sql_instance": cfg.sql_instance,
                "aliases": [a.to_dict() for a in cfg.aliases],
                "active_aliases": cfg.active_aliases,
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


# --- series para gráficas (RF-5) ----------------------------------------------------------

_RANGES: dict[str, tuple[int, int, int]] = {
    # rango: (horas, bucket latencia s, bucket disponibilidad s)
    "24h": (24, 600, 3600),
    "7d": (24 * 7, 3600, 6 * 3600),
    "30d": (24 * 30, 4 * 3600, 24 * 3600),
}


@router.get("/connections/{connection_id}/series")
def connection_series(request: Request, connection_id: int, range: str = "24h") -> dict[str, Any]:
    ctx = _ctx(request)
    _get_or_404(ctx, connection_id)
    hours, lat_bucket, avail_bucket = _RANGES.get(range, _RANGES["24h"])
    now = utc_now()
    since = now - timedelta(hours=hours)
    rows = ctx.db.list_checks(connection_id, to_iso(since))

    from app.util import from_iso

    lat_sum: dict[int, float] = {}
    lat_n: dict[int, int] = {}
    avail: dict[int, dict[str, int]] = {}
    base = since.timestamp()
    for row in rows:
        ts = from_iso(row["ts_utc"]).timestamp()
        if row["latency_ms"] is not None:
            bucket = int((ts - base) // lat_bucket)
            lat_sum[bucket] = lat_sum.get(bucket, 0.0) + row["latency_ms"]
            lat_n[bucket] = lat_n.get(bucket, 0) + 1
        abucket = int((ts - base) // avail_bucket)
        counts = avail.setdefault(abucket, {"UP": 0, "DEGRADED": 0, "DOWN": 0})
        counts[row["status"]] = counts.get(row["status"], 0) + 1

    def bucket_ts(index: int, size: int) -> str:
        return to_iso(since + timedelta(seconds=index * size))

    latency = [
        {"t": bucket_ts(b, lat_bucket), "ms": round(lat_sum[b] / lat_n[b], 1)}
        for b in sorted(lat_sum)
    ]
    availability = [
        {"t": bucket_ts(b, avail_bucket), **avail[b]} for b in sorted(avail)
    ]
    return {"range": range, "latency": latency, "availability": availability}


# --- export CSV (RF-3) ----------------------------------------------------------------------


def _csv_response(header: list[str], rows: Any, filename: str) -> Response:
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/checks.csv")
def export_checks(request: Request, connection_id: int | None = None, days: int = 30) -> Response:
    ctx = _ctx(request)
    since = to_iso(utc_now() - timedelta(days=max(1, min(days, 3650))))
    meta = {c.id: (c.name, " | ".join(c.active_aliases)) for c in ctx.db.list_connections()}
    if connection_id is not None:
        rows = ctx.db.list_checks(connection_id, since)
    else:
        rows = ctx.db.execute(
            "SELECT * FROM checks WHERE ts_utc >= ? ORDER BY ts_utc", (since,)
        ).fetchall()
    data = (
        (
            r["id"], r["connection_id"], meta.get(r["connection_id"], ("", ""))[0], r["ts_utc"],
            r["status"], r["latency_ms"], r["error_type"] or "", r["error_msg"],
            meta.get(r["connection_id"], ("", ""))[1],
        )
        for r in rows
    )
    header = ["id", "connection_id", "connection", "ts_utc", "status", "latency_ms",
              "error_type", "error_msg", "aliases"]
    return _csv_response(header, data, "checks.csv")


@router.get("/export/incidents.csv")
def export_incidents(request: Request, connection_id: int | None = None) -> Response:
    ctx = _ctx(request)
    meta = {c.id: (c.name, " | ".join(c.active_aliases)) for c in ctx.db.list_connections()}
    rows = ctx.db.list_incidents(connection_id)
    data = (
        (
            r["id"], r["connection_id"], meta.get(r["connection_id"], ("", ""))[0], r["started_at"],
            r["ended_at"] or "", r["duration_s"] or "", r["error_type"] or "",
            r["first_error_msg"], meta.get(r["connection_id"], ("", ""))[1],
        )
        for r in rows
    )
    header = ["id", "connection_id", "connection", "started_at", "ended_at",
              "duration_s", "error_type", "first_error_msg", "aliases"]
    return _csv_response(header, data, "incidents.csv")


# --- reportes (RF-6) --------------------------------------------------------------------------


@router.get("/reports")
def list_reports(request: Request) -> list[dict[str, Any]]:
    from app import config as app_config

    out = []
    for path in sorted(app_config.reports_dir().glob("*.html"), reverse=True):
        stat = path.stat()
        pdf_path = path.with_suffix(".pdf")
        out.append(
            {
                "file": path.name,
                "pdf_file": pdf_path.name if pdf_path.is_file() else None,
                "pdf_size": pdf_path.stat().st_size if pdf_path.is_file() else None,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            }
        )
    return out


@router.post("/reports", status_code=201)
def create_report(request: Request, payload: dict[str, str]) -> dict[str, str]:
    from datetime import date

    from app.reports import Branding, generate_report
    from app.settings_store import get_str

    ctx = _ctx(request)
    client = (payload.get("client") or "").strip()
    try:
        date_from = date.fromisoformat(payload.get("date_from", ""))
        date_to = date.fromisoformat(payload.get("date_to", ""))
    except ValueError:
        raise HTTPException(status_code=422, detail=["Fechas inválidas (usa AAAA-MM-DD)."])
    branding = Branding(
        company=get_str(ctx.db, "branding.company"),
        accent=get_str(ctx.db, "branding.accent"),
        logo_b64=get_str(ctx.db, "branding.logo_b64"),
    )
    try:
        path = generate_report(ctx.db, client, date_from, date_to, branding)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=[str(exc)])
    pdf_path = path.with_suffix(".pdf")
    return {"file": path.name, "pdf_file": pdf_path.name if pdf_path.is_file() else ""}


# --- ajustes (RF-7) ---------------------------------------------------------------------


_NUMERIC_SETTING_BOUNDS: dict[str, tuple[float, float]] = {
    "courtesy.global_concurrency": (1, 50),
    "courtesy.host_spacing_s": (0, 300),
    "courtesy.host_max_checks_per_min": (1, 60),
    "courtesy.backoff_cap_s": (30, 3600),
    "courtesy.jitter_ratio": (0, 0.5),
    "retention.days": (1, 3650),
    "alerts.reminder_minutes": (0, 1440),
    "smtp.port": (1, 65535),
}


@router.get("/settings")
def get_settings(request: Request) -> dict[str, Any]:
    from app.settings_store import DEFAULTS, get_str

    ctx = _ctx(request)
    out: dict[str, Any] = {}
    for key in DEFAULTS:
        value = get_str(ctx.db, key)
        if key == "smtp.password":
            out[key] = ""  # nunca sale del servidor
            out["smtp.has_password"] = bool(value)
        else:
            out[key] = value
    return out


@router.put("/settings")
def put_settings(request: Request, payload: dict[str, Any]) -> dict[str, str]:
    from app.settings_store import DEFAULTS, courtesy_policy

    ctx = _ctx(request)
    errors: list[str] = []
    for key, raw in payload.items():
        if key not in DEFAULTS:
            errors.append(f"Ajuste desconocido: {key}")
            continue
        value = "1" if raw is True else "0" if raw is False else str(raw).strip()
        bounds = _NUMERIC_SETTING_BOUNDS.get(key)
        if bounds is not None:
            try:
                number = float(value)
            except ValueError:
                errors.append(f"{key}: debe ser numérico.")
                continue
            if not (bounds[0] <= number <= bounds[1]):
                errors.append(f"{key}: debe estar entre {bounds[0]} y {bounds[1]}.")
                continue
        if key == "smtp.password":
            if value == "":
                continue  # vacío = conservar la guardada
            value = _encrypt_secret(ctx, value) or ""
        ctx.db.set_setting(key, value)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    # Los parámetros de cortesía aplican en caliente (salvo la concurrencia
    # global, que requiere reinicio: el semáforo se crea al arrancar).
    ctx.throttle.policy = courtesy_policy(ctx.db)
    return {"status": "ok"}


# --- backup / restore de configuración (RF-7) -------------------------------------------


@router.get("/backup")
def export_backup(request: Request) -> Response:
    """Config export. Secrets are excluded by design (DPAPI/Fernet no viajan)."""
    import json

    from app import __version__
    from app.settings_store import DEFAULTS, get_str

    ctx = _ctx(request)
    settings = {
        key: get_str(ctx.db, key) for key in DEFAULTS if key != "smtp.password"
    }
    connections = []
    for cfg in ctx.db.list_connections():
        params = cfg.to_params()
        params.pop("secret_encrypted", None)
        params.pop("id", None)
        connections.append(params)
    payload = {
        "app": "StabilityMonitor",
        "version": __version__,
        "exported_at": to_iso(utc_now()),
        "note": "Los secretos no se exportan: deberán reingresarse al restaurar.",
        "settings": settings,
        "connections": connections,
    }
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="monitor-backup.json"'},
    )


@router.post("/restore")
def import_backup(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    import json

    from app.models import DEFAULT_PORTS, Protocol
    from app.settings_store import DEFAULTS

    ctx = _ctx(request)
    if payload.get("app") != "StabilityMonitor":
        raise HTTPException(status_code=422, detail=["El archivo no es un backup de StabilityMonitor."])

    applied_settings = 0
    for key, value in (payload.get("settings") or {}).items():
        if key in DEFAULTS and key != "smtp.password":
            ctx.db.set_setting(key, str(value))
            applied_settings += 1

    existing = {
        (c.protocol.value, c.host, c.port, c.sql_instance or "", c.name)
        for c in ctx.db.list_connections()
    }
    created = 0
    skipped = 0
    secrets_imported = 0
    for item in payload.get("connections") or []:
        try:
            aliases_raw = item.get("aliases_json")
            if aliases_raw is None and "aliases" in item:
                aliases_raw = json.dumps(item.get("aliases") or [])
            if not isinstance(aliases_raw, str):
                aliases_raw = json.dumps(aliases_raw or [])
            protocol = Protocol(item["protocol"])
            sql_instance = (
                item.get("sql_instance")
                or item.get("instance")
                or item.get("database_instance")
                or None
            )
            sql_instance = str(sql_instance).strip() if sql_instance else None
            raw_port = item.get("port")
            if (raw_port is None or raw_port == "") and protocol is Protocol.SQLSERVER and sql_instance:
                port = 0
            elif raw_port is None or raw_port == "":
                port = DEFAULT_PORTS[protocol]
            else:
                port = int(raw_port)
            cfg = ConnectionConfig(
                id=None,
                name=item["name"],
                client=item.get("client", ""),
                protocol=protocol,
                host=item["host"],
                port=port,
                username=item.get("username", ""),
                auth_type=item.get("auth_type", "password"),
                key_path=item.get("key_path"),
                db_name=item.get("db_name"),
                sql_instance=sql_instance,
                ssl_mode=item.get("ssl_mode", "preferred"),
                targets=json.loads(item.get("targets_json", "[]")),
                aliases=parse_aliases_json(aliases_raw),
                health_query=item.get("health_query"),
                interval_s=int(item.get("interval_s", 60)),
                timeout_s=float(item.get("timeout_s", 10)),
                retries=int(item.get("retries", 2)),
                degraded_ms=item.get("degraded_ms"),
                write_check=bool(item.get("write_check", 0)),
                enabled=False,  # sin secreto no puede autenticar: nace pausada
                notes=item.get("notes", ""),
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=[f"Conexión inválida en el backup: {exc}"])
        cfg.aliases = _merge_alias_metadata(cfg.aliases)
        if (cfg.protocol.value, cfg.host, cfg.port, cfg.sql_instance or "", cfg.name) in existing:
            skipped += 1
            continue
        if validate_connection(cfg):
            skipped += 1
            continue
        plain_secret = _import_plain_secret(item)
        if plain_secret:
            cfg.secret_encrypted = _encrypt_secret(ctx, plain_secret)
            secrets_imported += 1
        ctx.db.create_connection(cfg)
        existing.add((cfg.protocol.value, cfg.host, cfg.port, cfg.sql_instance or "", cfg.name))
        created += 1

    return {
        "connections_created": created,
        "connections_skipped": skipped,
        "secrets_imported": secrets_imported,
        "settings_applied": applied_settings,
        "warning": "Las conexiones restauradas quedan en pausa. Si el archivo incluía contraseñas, se cifraron localmente; reanuda las conexiones tras verificarlas.",
    }


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
