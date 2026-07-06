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
import secrets as pysecrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from app import __version__, config
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


def resolve_auth(mode: str) -> DashboardAuth:
    user = os.environ.get("MONITOR_DASH_USER", "")
    password = os.environ.get("MONITOR_DASH_PASS", "")
    if mode == "docker":
        if not user or not password:
            raise RuntimeError(
                "En modo Docker el dashboard exige autenticación: define "
                "MONITOR_DASH_USER y MONITOR_DASH_PASS en el entorno."
            )
        return DashboardAuth(enabled=True, username=user, password=password)
    if user and password:
        return DashboardAuth(enabled=True, username=user, password=password)
    return DashboardAuth(enabled=False)


def build_context(mode: str | None = None, with_engine: bool = True) -> AppContext:
    mode = mode or runtime_mode()
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
    engine = MonitorEngine(db, tracker, throttle, secret_store) if with_engine else None
    return AppContext(
        db=db,
        tracker=tracker,
        throttle=throttle,
        engine=engine,
        secret_store=secret_store,
        auth=resolve_auth(mode),
        mode=mode,
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

    @app.get("/healthz")
    def healthz() -> Response:
        # No auth: used by the Docker healthcheck. Reveals liveness only.
        engine = app.state.ctx.engine
        if engine is None or engine.is_alive():
            return JSONResponse({"status": "ok", "version": __version__})
        return JSONResponse({"status": "scheduler caído"}, status_code=503)

    return app


def main() -> None:
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
    app = create_app()
    logger.info("StabilityMonitor %s — modo %s — http://%s:%d", __version__, mode, host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
