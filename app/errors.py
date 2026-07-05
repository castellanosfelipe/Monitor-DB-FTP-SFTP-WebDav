"""Failure taxonomy and centralized exception classification.

Every checker funnels its failures through this module so that the same real
world condition always maps to the same ``ErrorType`` regardless of protocol.
The taxonomy distinguishes "the server is down" from "the client's path or
table disappeared" — that difference is what makes the reports useful.
"""
from __future__ import annotations

import errno
import socket
import ssl
from enum import Enum

try:  # httpx is a runtime dependency, but keep classification importable without it
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

MAX_ERROR_LEN = 500

_UNREACHABLE_ERRNOS = {
    errno.EHOSTUNREACH,
    errno.ENETUNREACH,
    errno.ENETDOWN,
    getattr(errno, "EHOSTDOWN", -1),
}


class ErrorType(str, Enum):
    """Stored in ``checks.error_type`` / ``incidents.error_type``."""

    DNS = "dns"
    TCP_CONNECT = "tcp_connect"
    TCP_TIMEOUT = "tcp_timeout"
    TLS = "tls"
    AUTH = "auth"
    TARGET_MISSING = "target_missing"
    DB_MISSING = "db_missing"
    PERMISSION = "permission"
    QUERY_TIMEOUT = "query_timeout"
    LATENCY = "latency"
    PROTOCOL = "protocol"
    UNKNOWN = "unknown"


class CheckError(Exception):
    """A classified, connection-level failure raised inside a checker."""

    def __init__(self, error_type: ErrorType, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message


def truncate(message: str, limit: int = MAX_ERROR_LEN) -> str:
    message = " ".join(message.split())  # collapse newlines from server banners
    return message if len(message) <= limit else message[: limit - 1] + "…"


def _causes(exc: BaseException):
    """Walk the exception chain (wrapper libraries bury the real cause)."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        yield current
        seen.add(id(current))
        current = current.__cause__ or current.__context__


def _classify_stdlib(exc: BaseException) -> tuple[ErrorType, str] | None:
    if isinstance(exc, socket.gaierror):
        return ErrorType.DNS, "no se pudo resolver el nombre de host"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return ErrorType.TLS, f"certificado no válido: {exc}"
    if isinstance(exc, ssl.SSLError):
        return ErrorType.TLS, f"error TLS: {getattr(exc, 'reason', None) or exc}"
    if isinstance(exc, TimeoutError):  # includes socket.timeout
        return ErrorType.TCP_TIMEOUT, "tiempo de espera agotado al conectar u operar"
    if isinstance(exc, ConnectionRefusedError):
        return ErrorType.TCP_CONNECT, "conexión rechazada por el servidor"
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return ErrorType.TCP_CONNECT, "la conexión fue interrumpida"
    if isinstance(exc, OSError) and exc.errno in _UNREACHABLE_ERRNOS:
        return ErrorType.TCP_CONNECT, "host o red inalcanzable"
    if isinstance(exc, OSError) and exc.errno == errno.ETIMEDOUT:
        return ErrorType.TCP_TIMEOUT, "tiempo de espera agotado al conectar"
    return None


def _sniff_network_text(text: str) -> tuple[ErrorType, str]:
    lowered = text.lower()
    if any(k in lowered for k in ("getaddrinfo", "nodename", "name or service", "not known")):
        return ErrorType.DNS, "no se pudo resolver el nombre de host"
    if any(k in lowered for k in ("certificate", "ssl", "tls")):
        return ErrorType.TLS, truncate(f"error TLS: {text}")
    if "refused" in lowered:
        return ErrorType.TCP_CONNECT, "conexión rechazada por el servidor"
    if "timed out" in lowered or "timeout" in lowered:
        return ErrorType.TCP_TIMEOUT, "tiempo de espera agotado"
    return ErrorType.TCP_CONNECT, truncate(text) or "fallo de conexión"


def classify_exception(exc: BaseException) -> tuple[ErrorType, str]:
    """Map any exception raised during a check to ``(ErrorType, message)``."""
    if isinstance(exc, CheckError):
        return exc.error_type, truncate(exc.message)
    if httpx is not None and isinstance(exc, httpx.TimeoutException):
        return ErrorType.TCP_TIMEOUT, "tiempo de espera agotado"
    for cause in _causes(exc):
        hit = _classify_stdlib(cause)
        if hit is not None:
            return hit[0], truncate(hit[1])
    if httpx is not None and isinstance(exc, httpx.HTTPError):
        if isinstance(exc, httpx.RemoteProtocolError):
            return ErrorType.PROTOCOL, truncate(f"error de protocolo HTTP: {exc}")
        return _sniff_network_text(str(exc))
    return ErrorType.UNKNOWN, truncate(f"{type(exc).__name__}: {exc}")
