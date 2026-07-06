"""FastAPI application: dashboard, API, healthz, scheduler lifecycle.

Modes (see docs):
- windows/dev: binds 127.0.0.1; Basic Auth optional (only if MONITOR_DASH_USER
  and MONITOR_DASH_PASS are set). Set MONITOR_BIND_LAN=1 to listen on the LAN
  (a warning is logged: enable auth!).
- docker: binds 0.0.0.0 and Basic Auth is mandatory — the app refuses to start
  without MONITOR_DASH_USER / MONITOR_DASH_PASS.
"""
from __future__ import annotations

import logging
import os
import re
import secrets as pysecrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from app import __version__, config
from app.alerts import Alerter, HeadlessNotifier, Notifier
from app.api.routes import router as api_router
from app.db import Database
from app.incidents import IncidentTracker
from app.logging_setup import setup_logging
from app.platform.detect import runtime_mode
from app.platform.secretstore import SecretStore, SecretStoreError, get_secret_store
from app.scheduler import MonitorEngine
from app.settings_store import DashboardAuth, courtesy_policy
from app.throttle import Throttle

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass
class AppContext:
    db: Database
    tracker: IncidentTracker
    throttle: Throttle
    engine: MonitorEngine | None
    secret_store: SecretStore | None
    auth: DashboardAuth
    mode: str
    alerter: Alerter | None = None
    notifier: Notifier | None = None


def resolve_auth(mode: str) -> DashboardAuth:
    user = os.environ.get("MONITOR_DASH_USER", "")
    password = os.environ.get("MONITOR_DASH_PASS", "")
    if mode in ("docker", "serverless"):
        # Expuesto a red (o a internet): la autenticación es obligatoria.
        if not user or not password:
            raise RuntimeError(
                f"En modo {mode} el dashboard exige autenticación: define "
                "MONITOR_DASH_USER y MONITOR_DASH_PASS en el entorno."
            )
        return DashboardAuth(enabled=True, username=user, password=password)
    if user and password:
        return DashboardAuth(enabled=True, username=user, password=password)
    return DashboardAuth(enabled=False)


def build_context(mode: str | None = None, with_engine: bool = True) -> AppContext:
    mode = mode or runtime_mode()
    if mode == "serverless":
        from app.db_pg import PgDatabase

        dsn = os.environ.get("DATABASE_URL", "").strip()
        if not dsn:
            raise RuntimeError(
                "En modo serverless se requiere DATABASE_URL (cadena de conexión "
                "de Neon/PostgreSQL)."
            )
        db: Database = PgDatabase(dsn)  # type: ignore[assignment]
        with_engine = False  # sin scheduler residente: los chequeos van por cron
    else:
        db = Database(config.db_path())
    tracker = IncidentTracker(db)
    throttle = Throttle(courtesy_policy(db))
    try:
        secret_store: SecretStore | None = get_secret_store(mode)
    except SecretStoreError as exc:
        if mode == "docker":
            raise
        logger.warning("almacén de secretos no disponible: %s", exc)
        secret_store = None
    if mode == "windows":
        from app.platform.notify_windows import WindowsNotifier

        notifier: Notifier = WindowsNotifier(db)
    else:
        notifier = HeadlessNotifier()
    alerter = Alerter(db, notifier)
    engine = (
        MonitorEngine(
            db,
            tracker,
            throttle,
            secret_store,
            event_sink=alerter.handle_events,
            housekeeping=[(60, alerter.check_reminders)],
        )
        if with_engine
        else None
    )
    return AppContext(
        db=db,
        tracker=tracker,
        throttle=throttle,
        engine=engine,
        secret_store=secret_store,
        auth=resolve_auth(mode),
        mode=mode,
        alerter=alerter,
        notifier=notifier,
    )


def _make_auth_dependency(app: FastAPI):
    security = HTTPBasic(auto_error=False)

    def dependency(
        request: Request, credentials: HTTPBasicCredentials | None = Depends(security)
    ) -> None:
        auth: DashboardAuth = request.app.state.ctx.auth
        if not auth.enabled:
            return
        if credentials is not None:
            user_ok = pysecrets.compare_digest(credentials.username, auth.username)
            pass_ok = pysecrets.compare_digest(credentials.password, auth.password)
            if user_ok and pass_ok:
                return
        raise HTTPException(
            status_code=401,
            detail="Credenciales requeridas.",
            headers={"WWW-Authenticate": 'Basic realm="StabilityMonitor"'},
        )

    return dependency


def create_app(ctx: AppContext | None = None) -> FastAPI:
    context = ctx or build_context()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if context.engine is not None:
            context.engine.start()
        yield
        if context.engine is not None:
            context.engine.stop()

    app = FastAPI(title="StabilityMonitor", version=__version__, lifespan=lifespan)
    app.state.ctx = context
    auth_dep = _make_auth_dependency(app)

    app.include_router(api_router, dependencies=[Depends(auth_dep)])
    app.mount("/static", StaticFiles(directory=_BASE_DIR / "static"), name="static")

    index_html = (_BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, _: None = Depends(auth_dep)) -> HTMLResponse:
        return HTMLResponse(index_html)

    @app.get("/reports/{filename}")
    def get_report(filename: str, _: None = Depends(auth_dep)) -> Response:
        # Nombre estricto: los reportes los genera la app, nada de rutas arbitrarias.
        if not re.fullmatch(r"[A-Za-z0-9._-]+\.html", filename):
            raise HTTPException(status_code=404, detail="Reporte no encontrado.")
        path = config.reports_dir() / filename
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Reporte no encontrado.")
        return FileResponse(path, media_type="text/html")

    @app.get("/healthz")
    def healthz() -> Response:
        # No auth: used by the Docker healthcheck. Reveals liveness only.
        engine = app.state.ctx.engine
        if engine is None or engine.is_alive():
            return JSONResponse({"status": "ok", "version": __version__})
        return JSONResponse({"status": "scheduler caído"}, status_code=503)

    @app.api_route("/api/cron/tick", methods=["GET", "POST"])
    def cron_tick(request: Request) -> Response:
        """Serverless check runner (Vercel Cron o un scheduler externo).

        Protegido por ``CRON_SECRET`` (header ``Authorization: Bearer <secreto>``,
        que Vercel envía automáticamente a sus crons). Sin secreto configurado
        solo se permite fuera de serverless (desarrollo local).
        """
        secret = os.environ.get("CRON_SECRET", "")
        if secret:
            provided = request.headers.get("authorization", "")
            if not pysecrets.compare_digest(provided, f"Bearer {secret}"):
                raise HTTPException(status_code=401, detail="Credenciales requeridas.")
        elif app.state.ctx.mode == "serverless":
            raise HTTPException(
                status_code=503,
                detail="Define CRON_SECRET para habilitar el tick de chequeos.",
            )
        from app.serverless import run_due_checks

        return JSONResponse(run_due_checks(app.state.ctx))

    return app


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m app.main", description="StabilityMonitor")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="siembra conexiones ficticias con 30 días de historial sintético",
    )
    args = parser.parse_args()

    mode = runtime_mode()
    setup_logging(mode)
    port = int(os.environ.get("MONITOR_PORT", str(config.DEFAULT_PORT)))
    if mode == "docker":
        host = "0.0.0.0"
    elif os.environ.get("MONITOR_BIND_LAN") == "1":
        host = "0.0.0.0"
        logger.warning(
            "El dashboard escuchará en toda la LAN. Configura MONITOR_DASH_USER/"
            "MONITOR_DASH_PASS para exigir autenticación."
        )
    else:
        host = "127.0.0.1"
    context = build_context(mode)
    if args.demo or os.environ.get("MONITOR_DEMO") == "1":
        from app.demo import seed_demo

        created = seed_demo(context.db)
        if created:
            logger.info("modo demo: %d conexiones ficticias sembradas", created)
    app = create_app(context)
    ctx: AppContext = app.state.ctx
    if mode == "windows" and ctx.engine is not None:
        try:
            from app.platform.tray_windows import TrayApp

            def _quit() -> None:
                ctx.engine.stop()
                os._exit(0)

            tray = TrayApp(ctx.engine, port, on_quit=_quit)
            if ctx.notifier is not None and hasattr(ctx.notifier, "on_state_change"):
                ctx.notifier.on_state_change = tray.set_state
            tray.start()
        except Exception:
            logger.exception("no se pudo iniciar el ícono de bandeja")
    logger.info("StabilityMonitor %s — modo %s — http://%s:%d", __version__, mode, host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
