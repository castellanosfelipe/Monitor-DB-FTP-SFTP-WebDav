"""Error classification tests: same real-world condition → same ErrorType."""
from __future__ import annotations

import errno
import socket
import ssl
from ftplib import error_perm

import httpx
import pytest

from app.checkers.ftp import classify_target_error
from app.checkers.webdav import status_error
from app.errors import CheckError, ErrorType, classify_exception, truncate


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (socket.gaierror(8, "nodename nor servname provided"), ErrorType.DNS),
        (TimeoutError("timed out"), ErrorType.TCP_TIMEOUT),
        (socket.timeout("timed out"), ErrorType.TCP_TIMEOUT),
        (ConnectionRefusedError(errno.ECONNREFUSED, "refused"), ErrorType.TCP_CONNECT),
        (ConnectionResetError(errno.ECONNRESET, "reset"), ErrorType.TCP_CONNECT),
        (ssl.SSLError(1, "TLSV1_ALERT_PROTOCOL_VERSION"), ErrorType.TLS),
        (OSError(errno.EHOSTUNREACH, "no route to host"), ErrorType.TCP_CONNECT),
        (OSError(errno.ETIMEDOUT, "timed out"), ErrorType.TCP_TIMEOUT),
        (ValueError("totally unexpected"), ErrorType.UNKNOWN),
    ],
)
def test_stdlib_classification(exc: BaseException, expected: ErrorType):
    error_type, message = classify_exception(exc)
    assert error_type is expected
    assert message


def test_check_error_passes_through():
    error_type, message = classify_exception(CheckError(ErrorType.AUTH, "credenciales inválidas"))
    assert error_type is ErrorType.AUTH
    assert message == "credenciales inválidas"


def test_wrapped_cause_is_found():
    inner = socket.gaierror(8, "unknown host")
    outer = RuntimeError("wrapper")
    outer.__cause__ = inner
    error_type, _ = classify_exception(outer)
    assert error_type is ErrorType.DNS


def test_httpx_timeout_classification():
    error_type, _ = classify_exception(httpx.ConnectTimeout("timed out"))
    assert error_type is ErrorType.TCP_TIMEOUT


def test_httpx_connect_error_sniffing():
    error_type, _ = classify_exception(httpx.ConnectError("[Errno 61] Connection refused"))
    assert error_type is ErrorType.TCP_CONNECT


def test_ftp_target_error_classification():
    missing = error_perm("550 No such file or directory")
    denied = error_perm("550 Permission denied")
    assert classify_target_error(missing)[0] is ErrorType.TARGET_MISSING
    assert classify_target_error(denied)[0] is ErrorType.PERMISSION


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (200, None),
        (207, None),
        (301, None),
        (401, ErrorType.AUTH),
        (403, ErrorType.PERMISSION),
        (404, ErrorType.TARGET_MISSING),
        (410, ErrorType.TARGET_MISSING),
        (500, ErrorType.PROTOCOL),
    ],
)
def test_webdav_status_mapping(code: int, expected: ErrorType | None):
    result = status_error(code)
    if expected is None:
        assert result is None
    else:
        assert result is not None and result[0] is expected


def test_truncate_collapses_and_limits():
    noisy = "línea 1\n   línea 2\t\tlínea 3"
    assert truncate(noisy) == "línea 1 línea 2 línea 3"
    long_message = "x" * 1000
    assert len(truncate(long_message)) == 500
