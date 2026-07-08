"""Minimal test CLI (Fase 1): run one check and print the detailed result.

Usage:
    python -m app.check <connection_id>          # connection stored in data/monitor.db
    python -m app.check --file conn.json         # ad-hoc check, no database needed
    python -m app.check --file conn.json --ask-secret

JSON file keys (only ``protocol`` and ``host`` are required)::

    {
      "protocol": "SFTP", "host": "10.0.0.5", "port": 22,
      "username": "monitor", "secret": "...",
      "auth_type": "password" | "key", "key_path": "C:/llaves/id_ed25519",
      "targets": ["/clientes/acme/entrada"],
      "timeout_s": 10, "ssl_mode": "preferred", "write_check": false,
      "db_name": null, "sql_instance": null, "health_query": null, "degraded_ms": null
    }

Exit codes: 0 = UP, 1 = DEGRADED, 2 = DOWN, 3 = configuration error.
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from app import config
from app.checkers import get_checker
from app.db import Database
from app.models import DEFAULT_PORTS, ConnectionConfig, Protocol, Status, validate_connection
from app.platform.secretstore import SecretStoreError, get_secret_store

_EXIT_BY_STATUS = {Status.UP: 0, Status.DEGRADED: 1, Status.DOWN: 2}


def _config_from_file(path: Path) -> tuple[ConnectionConfig, str | None]:
    data = json.loads(path.read_text(encoding="utf-8"))
    try:
        protocol = Protocol(str(data["protocol"]).upper())
    except (KeyError, ValueError):
        valid = ", ".join(p.value for p in Protocol)
        raise SystemExit(f"Protocolo inválido o ausente. Usa uno de: {valid}")
    sql_instance = (data.get("sql_instance") or data.get("instance") or "").strip() or None
    if data.get("port") in (None, "") and protocol is Protocol.SQLSERVER and sql_instance:
        port = 0
    else:
        port = int(data.get("port") or DEFAULT_PORTS[protocol])
    cfg = ConnectionConfig(
        id=None,
        name=data.get("name", f"{protocol.value} {data.get('host', '?')}"),
        client=data.get("client", ""),
        protocol=protocol,
        host=data.get("host", ""),
        port=port,
        username=data.get("username", ""),
        auth_type=data.get("auth_type", "password"),
        key_path=data.get("key_path"),
        db_name=data.get("db_name"),
        sql_instance=sql_instance,
        ssl_mode=data.get("ssl_mode", "preferred"),
        targets=list(data.get("targets", [])),
        health_query=data.get("health_query"),
        timeout_s=float(data.get("timeout_s", 10)),
        degraded_ms=data.get("degraded_ms"),
        write_check=bool(data.get("write_check", False)),
    )
    return cfg, data.get("secret")


def _config_from_db(connection_id: int) -> tuple[ConnectionConfig, str | None]:
    db = Database(config.db_path())
    cfg = db.get_connection(connection_id)
    if cfg is None:
        raise SystemExit(f"No existe una conexión con id {connection_id} en {config.db_path()}")
    secret: str | None = None
    if cfg.secret_encrypted:
        try:
            secret = get_secret_store().decrypt(cfg.secret_encrypted)
        except SecretStoreError as exc:
            raise SystemExit(f"Error al descifrar el secreto: {exc}")
    return cfg, secret


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.check",
        description="Ejecuta un chequeo puntual y muestra el resultado detallado.",
    )
    parser.add_argument("connection_id", nargs="?", type=int, help="id de la conexión en la BD")
    parser.add_argument("--file", type=Path, help="archivo JSON con la definición de la conexión")
    parser.add_argument(
        "--ask-secret", action="store_true", help="pedir el secreto por consola (no queda en el JSON)"
    )
    args = parser.parse_args(argv)

    if (args.connection_id is None) == (args.file is None):
        parser.error("indica un id de conexión o --file (exactamente uno)")

    if args.file is not None:
        cfg, secret = _config_from_file(args.file)
    else:
        cfg, secret = _config_from_db(args.connection_id)

    if args.ask_secret:
        secret = getpass.getpass("Secreto (contraseña o passphrase): ")

    problems = validate_connection(cfg)
    if problems:
        print("La configuración no es válida:")
        for problem in problems:
            print(f"  - {problem}")
        return 3

    print(f"Conexión: {cfg.name} [{cfg.protocol.value}] {cfg.host}:{cfg.port}")
    result = get_checker(cfg.protocol).check(cfg, secret)

    print(f"Estado:   {result.status.value}")
    if result.latency_ms is not None:
        print(f"Latencia: {result.latency_ms:.0f} ms")
    if result.error_type is not None:
        print(f"Causa:    {result.error_type.value} — {result.error_msg}")
    if result.targets:
        print("Objetivos:")
        for target in result.targets:
            mark = "OK   " if target.ok else "FALLO"
            detail = f" — {target.message}" if target.message else ""
            print(f"  [{mark}] {target.target}{detail}")
    return _EXIT_BY_STATUS[result.status]


if __name__ == "__main__":
    sys.exit(main())
