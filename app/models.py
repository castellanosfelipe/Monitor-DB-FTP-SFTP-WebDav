"""Domain models: protocols, statuses, connection config, check results, validation."""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.errors import ErrorType


class Protocol(str, Enum):
    FTP = "FTP"
    FTPS = "FTPS"
    SFTP = "SFTP"
    WEBDAV = "WEBDAV"
    WEBDAVS = "WEBDAVS"
    POSTGRES = "POSTGRES"
    MYSQL = "MYSQL"
    MARIADB = "MARIADB"
    SQLSERVER = "SQLSERVER"
    ORACLE = "ORACLE"

    @property
    def is_file(self) -> bool:
        return self in FILE_PROTOCOLS

    @property
    def is_database(self) -> bool:
        return self in DB_PROTOCOLS


FILE_PROTOCOLS = frozenset(
    {Protocol.FTP, Protocol.FTPS, Protocol.SFTP, Protocol.WEBDAV, Protocol.WEBDAVS}
)
DB_PROTOCOLS = frozenset(
    {Protocol.POSTGRES, Protocol.MYSQL, Protocol.MARIADB, Protocol.SQLSERVER, Protocol.ORACLE}
)

DEFAULT_PORTS: dict[Protocol, int] = {
    Protocol.FTP: 21,
    Protocol.FTPS: 21,
    Protocol.SFTP: 22,
    Protocol.WEBDAV: 80,
    Protocol.WEBDAVS: 443,
    Protocol.POSTGRES: 5432,
    Protocol.MYSQL: 3306,
    Protocol.MARIADB: 3306,
    Protocol.SQLSERVER: 1433,
    Protocol.ORACLE: 1521,
}

SSL_MODES = ("disabled", "preferred", "required")
AUTH_TYPES = ("password", "key")

MIN_INTERVAL_S = 30
MAX_INTERVAL_S = 3600
MIN_TIMEOUT_S = 1
MAX_TIMEOUT_S = 120
MAX_RETRIES = 10
HEALTH_QUERY_MAX_LEN = 2000
HEALTH_QUERY_TIMEOUT_S = 5  # hard cap (RF-2), enforced again at execution time
MAX_ALIAS_LEN = 120


class Status(str, Enum):
    UP = "UP"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"


@dataclass
class ConnectionAlias:
    """A local, logical name for one connection.

    Aliases are metadata only: they never participate in checker selection,
    socket creation or connection-string construction.
    """

    name: str
    enabled: bool = True
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_obj(cls, obj: Any) -> "ConnectionAlias":
        if isinstance(obj, str):
            return cls(name=obj)
        if isinstance(obj, dict):
            return cls(
                name=str(obj.get("name", "")),
                enabled=bool(obj.get("enabled", True)),
                created_at=obj.get("created_at"),
                updated_at=obj.get("updated_at"),
            )
        return cls(name=str(obj or ""))

    def normalized(self) -> "ConnectionAlias":
        return ConnectionAlias(
            name=normalize_alias_name(self.name),
            enabled=self.enabled,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def normalize_alias_name(name: str) -> str:
    """Trim and normalize aliases to NFC so accents survive round-trips."""
    return unicodedata.normalize("NFC", str(name).strip())


def parse_aliases_json(value: str | None) -> list[ConnectionAlias]:
    try:
        raw = json.loads(value or "[]")
    except json.JSONDecodeError:
        raw = []
    if not isinstance(raw, list):
        return []
    return [ConnectionAlias.from_obj(item).normalized() for item in raw]


def aliases_to_json(aliases: list[ConnectionAlias]) -> str:
    normalized = [alias.normalized().to_dict() for alias in aliases if alias.name.strip()]
    return json.dumps(normalized, ensure_ascii=False)


def alias_key(name: str) -> str:
    return normalize_alias_name(name).casefold()


@dataclass
class TargetResult:
    """Outcome of verifying one target (a path, schema or table)."""

    target: str
    ok: bool
    error_type: ErrorType | None = None
    message: str = ""


@dataclass
class CheckResult:
    """Outcome of one full check of a connection."""

    status: Status
    latency_ms: float | None
    error_type: ErrorType | None = None
    error_msg: str = ""
    targets: list[TargetResult] = field(default_factory=list)


@dataclass
class ConnectionConfig:
    """One monitored connection. Mirrors the ``connections`` table."""

    id: int | None
    name: str
    client: str
    protocol: Protocol
    host: str
    port: int
    username: str = ""
    secret_encrypted: str | None = None
    auth_type: str = "password"
    key_path: str | None = None
    db_name: str | None = None
    sql_instance: str | None = None
    ssl_mode: str = "preferred"
    targets: list[str] = field(default_factory=list)
    aliases: list[ConnectionAlias] = field(default_factory=list)
    health_query: str | None = None
    interval_s: int = 60
    timeout_s: float = 10.0
    retries: int = 2
    degraded_ms: int | None = None
    write_check: bool = False
    enabled: bool = True
    notes: str = ""
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ConnectionConfig":
        return cls(
            id=row["id"],
            name=row["name"],
            client=row["client"],
            protocol=Protocol(row["protocol"]),
            host=row["host"],
            port=row["port"],
            username=row["username"],
            secret_encrypted=row["secret_encrypted"],
            auth_type=row["auth_type"],
            key_path=row["key_path"],
            db_name=row["db_name"],
            sql_instance=row["sql_instance"],
            ssl_mode=row["ssl_mode"],
            targets=json.loads(row["targets_json"] or "[]"),
            aliases=parse_aliases_json(row["aliases_json"] or "[]"),
            health_query=row["health_query"],
            interval_s=row["interval_s"],
            timeout_s=row["timeout_s"],
            retries=row["retries"],
            degraded_ms=row["degraded_ms"],
            write_check=bool(row["write_check"]),
            enabled=bool(row["enabled"]),
            notes=row["notes"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_params(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "client": self.client,
            "protocol": self.protocol.value,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "secret_encrypted": self.secret_encrypted,
            "auth_type": self.auth_type,
            "key_path": self.key_path,
            "db_name": self.db_name,
            "sql_instance": self.sql_instance,
            "ssl_mode": self.ssl_mode,
            "targets_json": json.dumps(self.targets, ensure_ascii=False),
            "aliases_json": aliases_to_json(self.aliases),
            "health_query": self.health_query,
            "interval_s": self.interval_s,
            "timeout_s": self.timeout_s,
            "retries": self.retries,
            "degraded_ms": self.degraded_ms,
            "write_check": int(self.write_check),
            "enabled": int(self.enabled),
            "notes": self.notes,
        }

    @property
    def active_aliases(self) -> list[str]:
        return [a.name for a in self.aliases if a.enabled]


# --- Validation -------------------------------------------------------------
# Messages are in Spanish because they surface directly in the UI/CLI.

_DB_TARGET_RE = re.compile(r"^[A-Za-z0-9_$#]+(\.[A-Za-z0-9_$#]+)?$")
_SQLSERVER_INSTANCE_RE = re.compile(r"^[A-Za-z0-9_$.-]{1,128}$")
_SELECT_RE = re.compile(r"^select\b", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_ALIAS_FORBIDDEN_CHARS = set('<>:"/\\|?*')
_ALIAS_INJECTION_RE = re.compile(
    r"(--|/\*|\*/|;|\bunion\s+select\b|\bdrop\s+table\b|\bor\s+1\s*=\s*1\b)",
    re.IGNORECASE,
)
_FORBIDDEN_SQL_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|merge"
    r"|exec|execute|call|into|attach|pragma|copy|vacuum|begin|commit|rollback"
    r"|lock|use|set|do)\b",
    re.IGNORECASE,
)


def validate_health_query(query: str) -> str | None:
    """Return a (Spanish) error message, or ``None`` if the query is acceptable.

    Enforced both when saving and immediately before executing (RF-2): the
    monitor must be physically unable to run anything but a trivial SELECT.
    """
    q = query.strip()
    if not q:
        return "La query de salud no puede estar vacía."
    if len(q) > HEALTH_QUERY_MAX_LEN:
        return f"La query de salud es demasiado larga (máximo {HEALTH_QUERY_MAX_LEN} caracteres)."
    if q.endswith(";"):
        q = q[:-1].rstrip()
    if ";" in q:
        return "La query de salud debe ser una sola sentencia (no se permite ';')."
    if not _SELECT_RE.match(q):
        return "La query de salud debe comenzar por SELECT."
    match = _FORBIDDEN_SQL_RE.search(q)
    if match:
        return f"La query de salud contiene una palabra no permitida: {match.group(0).upper()}."
    return None


def validate_connection(cfg: ConnectionConfig) -> list[str]:
    """Validate a connection config; returns a list of Spanish error messages."""
    errors: list[str] = []

    if not cfg.name.strip():
        errors.append("El nombre no puede estar vacío.")
    alias_seen: set[str] = set()
    for alias in cfg.aliases:
        name = normalize_alias_name(alias.name)
        if not name:
            errors.append("El alias virtual no puede estar vacío.")
            continue
        if len(name) > MAX_ALIAS_LEN:
            errors.append(f"El alias virtual '{name[:30]}...' supera {MAX_ALIAS_LEN} caracteres.")
        if _CONTROL_RE.search(name):
            errors.append(f"El alias virtual '{name}' contiene caracteres de control.")
        if any(ch in _ALIAS_FORBIDDEN_CHARS for ch in name):
            errors.append(f"El alias virtual '{name}' contiene caracteres reservados.")
        if "../" in name or "..\\" in name:
            errors.append(f"El alias virtual '{name}' contiene secuencias de traversal.")
        if _ALIAS_INJECTION_RE.search(name):
            errors.append(f"El alias virtual '{name}' contiene una secuencia no permitida.")
        key = alias_key(name)
        if key in alias_seen:
            errors.append(f"El alias virtual '{name}' está duplicado en la conexión.")
        alias_seen.add(key)
    if not cfg.host.strip():
        errors.append("El host no puede estar vacío.")
    elif re.search(r"\s|://", cfg.host):
        errors.append("El host no debe contener espacios ni esquema (usa solo el nombre o IP).")

    if cfg.sql_instance:
        if cfg.protocol is not Protocol.SQLSERVER:
            errors.append("La instancia solo aplica a conexiones SQLSERVER.")
        elif not _SQLSERVER_INSTANCE_RE.match(cfg.sql_instance):
            errors.append("La instancia SQL Server contiene caracteres no permitidos.")

    if cfg.protocol is Protocol.SQLSERVER and cfg.sql_instance and cfg.port == 0:
        pass
    elif not isinstance(cfg.port, int) or not (1 <= cfg.port <= 65535):
        errors.append("El puerto debe ser un número entre 1 y 65535.")
    if not (MIN_INTERVAL_S <= cfg.interval_s <= MAX_INTERVAL_S):
        errors.append(f"El intervalo debe estar entre {MIN_INTERVAL_S} s y {MAX_INTERVAL_S} s.")
    if not (MIN_TIMEOUT_S <= cfg.timeout_s <= MAX_TIMEOUT_S):
        errors.append(f"El timeout debe estar entre {MIN_TIMEOUT_S} s y {MAX_TIMEOUT_S} s.")
    if not (0 <= cfg.retries <= MAX_RETRIES):
        errors.append(f"Los reintentos deben estar entre 0 y {MAX_RETRIES}.")
    if cfg.degraded_ms is not None and cfg.degraded_ms <= 0:
        errors.append("El umbral de latencia (ms) debe ser un número positivo.")
    if cfg.ssl_mode not in SSL_MODES:
        errors.append(f"ssl_mode debe ser uno de: {', '.join(SSL_MODES)}.")

    if cfg.auth_type not in AUTH_TYPES:
        errors.append(f"El tipo de autenticación debe ser uno de: {', '.join(AUTH_TYPES)}.")
    elif cfg.auth_type == "key":
        if cfg.protocol is not Protocol.SFTP:
            errors.append("La autenticación por llave privada solo aplica a SFTP.")
        elif not (cfg.key_path or "").strip():
            errors.append("Debes indicar la ruta de la llave privada para autenticación por llave.")

    for target in cfg.targets:
        if cfg.protocol.is_file:
            if not target.startswith("/"):
                errors.append(f"El objetivo '{target}' debe ser una ruta absoluta (empezar por '/').")
        elif not _DB_TARGET_RE.match(target):
            errors.append(
                f"El objetivo '{target}' no es válido: usa 'esquema' o 'esquema.tabla' "
                "(letras, números, _, $ y #)."
            )

    if cfg.health_query:
        if not cfg.protocol.is_database:
            errors.append("La query de salud solo aplica a conexiones de bases de datos.")
        else:
            error = validate_health_query(cfg.health_query)
            if error:
                errors.append(error)

    if cfg.protocol in (Protocol.POSTGRES, Protocol.ORACLE) and not (cfg.db_name or "").strip():
        which = "base de datos" if cfg.protocol is Protocol.POSTGRES else "service name"
        errors.append(f"Para {cfg.protocol.value} es obligatorio indicar el {which}.")

    if cfg.write_check and not cfg.protocol.is_file:
        errors.append("El chequeo de escritura solo aplica a protocolos de archivos.")

    return errors
