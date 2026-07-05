"""WebDAV / WebDAVS checker (httpx, no heavy WebDAV libraries).

Per RF-2 the check is: ``PROPFIND`` with ``Depth: 0`` per target (never
recursive), or a single ``OPTIONS`` on the base URL when no targets are
configured. Traffic identifies itself with the ``StabilityMonitor/x.y.z``
User-Agent so administrators can recognize and filter it.
"""
from __future__ import annotations

from urllib.parse import quote

import httpx

from app import config
from app.checkers.base import BaseChecker
from app.errors import CheckError, ErrorType
from app.models import ConnectionConfig, Protocol, TargetResult
from app.util import to_iso, utc_now

PROBE_NAME = ".monitor_probe"

_PROPFIND_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>'
)
_PROPFIND_HEADERS = {"Depth": "0", "Content-Type": "application/xml"}


def status_error(code: int) -> tuple[ErrorType, str] | None:
    """Map an HTTP status to a failure cause; ``None`` means the check passed."""
    if code < 400:  # 2xx, 207 Multi-Status, and 3xx (the resource answered)
        return None
    if code == 401:
        return ErrorType.AUTH, "autenticación rechazada (401)"
    if code == 403:
        return ErrorType.PERMISSION, "permiso denegado (403)"
    if code in (404, 410):
        return ErrorType.TARGET_MISSING, f"la ruta no existe ({code})"
    return ErrorType.PROTOCOL, f"respuesta inesperada del servidor ({code})"


class WebDavChecker(BaseChecker):
    def _execute(self, cfg: ConnectionConfig, secret: str | None) -> list[TargetResult]:
        scheme = "https" if cfg.protocol is Protocol.WEBDAVS else "http"
        base_url = f"{scheme}://{cfg.host}:{cfg.port}"
        # Certificate verification only under ssl_mode='required' (LAN self-signed certs).
        verify = cfg.ssl_mode == "required"
        basic = httpx.BasicAuth(cfg.username, secret or "") if cfg.username else None
        digest = httpx.DigestAuth(cfg.username, secret or "") if cfg.username else None

        client = httpx.Client(
            timeout=cfg.timeout_s,
            verify=verify,
            headers={"User-Agent": config.USER_AGENT},
            follow_redirects=False,
        )
        try:
            results: list[TargetResult] = []
            if cfg.targets:
                for target in cfg.targets:
                    results.append(self._check_target(client, base_url, target, basic, digest))
            else:
                response = self._request(client, "OPTIONS", base_url + "/", basic, digest)
                error = status_error(response.status_code)
                if error is not None:
                    raise CheckError(*error)
            if cfg.write_check:
                results.append(self._write_probe(client, base_url, cfg, basic, digest))

            # A failed authentication is connection-level: the whole check is DOWN.
            for result in results:
                if not result.ok and result.error_type is ErrorType.AUTH:
                    raise CheckError(ErrorType.AUTH, result.message)
            return results
        finally:
            client.close()

    @staticmethod
    def _request(
        client: httpx.Client,
        method: str,
        url: str,
        basic: httpx.BasicAuth | None,
        digest: httpx.DigestAuth | None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """One request with Basic auth, retried once with Digest if challenged."""
        response = client.request(method, url, content=content, headers=headers, auth=basic)
        if (
            response.status_code == 401
            and digest is not None
            and "digest" in response.headers.get("www-authenticate", "").lower()
        ):
            response = client.request(method, url, content=content, headers=headers, auth=digest)
        return response

    def _check_target(
        self,
        client: httpx.Client,
        base_url: str,
        target: str,
        basic: httpx.BasicAuth | None,
        digest: httpx.DigestAuth | None,
    ) -> TargetResult:
        url = base_url + quote(target, safe="/")
        response = self._request(
            client, "PROPFIND", url, basic, digest,
            content=_PROPFIND_BODY, headers=_PROPFIND_HEADERS,
        )
        error = status_error(response.status_code)
        if error is not None:
            return TargetResult(target=target, ok=False, error_type=error[0], message=error[1])
        return TargetResult(target=target, ok=True)

    def _write_probe(
        self,
        client: httpx.Client,
        base_url: str,
        cfg: ConnectionConfig,
        basic: httpx.BasicAuth | None,
        digest: httpx.DigestAuth | None,
    ) -> TargetResult:
        """Optional ≤1 KB PUT+DELETE probe, always deleted (best effort) — RF-2."""
        directory = cfg.targets[0] if cfg.targets else ""
        path = f"{directory.rstrip('/')}/{PROBE_NAME}"
        url = base_url + quote(path, safe="/")
        label = f"{path} (escritura)"
        payload = f"stability-monitor probe {to_iso(utc_now())}\n".encode()
        try:
            response = self._request(client, "PUT", url, basic, digest, content=payload)
            error = status_error(response.status_code)
            if error is not None:
                error_type = ErrorType.PERMISSION if error[0] is ErrorType.PROTOCOL else error[0]
                return TargetResult(
                    target=label,
                    ok=False,
                    error_type=error_type,
                    message=f"sin permiso de escritura ({response.status_code})",
                )
        finally:
            try:
                client.request("DELETE", url, auth=basic)
            except Exception:
                pass
        return TargetResult(target=label, ok=True)
