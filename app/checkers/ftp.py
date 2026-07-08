"""FTP / explicit FTPS checker (stdlib ftplib).

Per RF-2 the check is: connect → login → per target ``CWD`` + ``NLST`` of that
directory only (no recursion, no downloads) → clean ``QUIT``.
"""
from __future__ import annotations

import ssl
from ftplib import FTP, FTP_TLS, error_perm, error_proto, error_temp
from io import BytesIO

from app.checkers.base import BaseChecker
from app.errors import CheckError, ErrorType
from app.models import ConnectionConfig, Protocol, TargetResult
from app.util import to_iso, utc_now

PROBE_NAME = ".monitor_probe"

_PERMISSION_HINTS = ("permission", "denied", "not authorized", "access is", "forbidden")
_FTP_CONTROL_ENCODINGS = ("utf-8", "cp1252", "latin-1")
_FTP_LISTING_ENCODINGS = ("utf-8", "cp1252", "latin-1")


def make_ssl_context(ssl_mode: str) -> ssl.SSLContext:
    """Certificate verification only under ssl_mode='required'.

    Self-signed certificates are the norm on LAN servers; 'preferred' (default)
    still encrypts but does not verify the chain.
    """
    ctx = ssl.create_default_context()
    if ssl_mode != "required":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def classify_target_error(exc: error_perm) -> tuple[ErrorType, str]:
    """550 covers both 'missing' and 'permission denied'; sniff the message."""
    lowered = str(exc).lower()
    if any(hint in lowered for hint in _PERMISSION_HINTS):
        return ErrorType.PERMISSION, "permiso denegado"
    return ErrorType.TARGET_MISSING, "la ruta no existe o no es accesible"


class FtpChecker(BaseChecker):
    def _execute(self, cfg: ConnectionConfig, secret: str | None) -> list[TargetResult]:
        ftp = self._connect(cfg, secret)
        results: list[TargetResult] = []
        try:
            for target in cfg.targets:
                results.append(self._check_target(ftp, target))
            if cfg.write_check:
                results.append(self._write_probe(ftp, cfg))
        finally:
            try:
                ftp.quit()
            except Exception:
                try:
                    ftp.close()
                except Exception:
                    pass
        return results

    def _connect(self, cfg: ConnectionConfig, secret: str | None) -> FTP:
        last_decode_error: UnicodeDecodeError | None = None
        for encoding in _FTP_CONTROL_ENCODINGS:
            ftp = self._new_client(cfg, encoding)
            try:
                ftp.connect(cfg.host, cfg.port, timeout=cfg.timeout_s)
                ftp.login(cfg.username or "", secret or "")
                if isinstance(ftp, FTP_TLS):
                    try:
                        ftp.prot_p()  # protect the data channel too
                    except error_perm:
                        pass  # server without PROT P; control channel is already TLS
                return ftp
            except UnicodeDecodeError as exc:
                last_decode_error = exc
                self._quiet_close(ftp)
                continue
            except error_perm as exc:
                self._quiet_close(ftp)
                raise CheckError(ErrorType.AUTH, f"autenticación rechazada: {exc}") from exc
            except Exception:
                self._quiet_close(ftp)
                raise
        raise CheckError(
            ErrorType.PROTOCOL,
            f"respuesta FTP no se pudo decodificar: {last_decode_error}",
        )

    @staticmethod
    def _new_client(cfg: ConnectionConfig, encoding: str) -> FTP:
        if cfg.protocol is Protocol.FTPS:
            return FTP_TLS(context=make_ssl_context(cfg.ssl_mode), encoding=encoding)
        return FTP(encoding=encoding)

    @staticmethod
    def _quiet_close(ftp: FTP) -> None:
        try:
            ftp.close()
        except Exception:
            pass

    @staticmethod
    def _check_target(ftp: FTP, target: str) -> TargetResult:
        try:
            FtpChecker._cwd_with_encoding_fallback(ftp, target)
        except error_perm as exc:
            error_type, message = classify_target_error(exc)
            return TargetResult(target=target, ok=False, error_type=error_type, message=message)
        except (error_temp, error_proto) as exc:
            return TargetResult(
                target=target,
                ok=False,
                error_type=ErrorType.PROTOCOL,
                message=f"error de protocolo: {exc}",
            )
        try:
            FtpChecker._safe_nlst(ftp)
        except error_perm:
            # Several FTP servers answer 550 to NLST on an *empty* directory;
            # CWD already proved the directory exists and is accessible.
            pass
        return TargetResult(target=target, ok=True)

    @staticmethod
    def _cwd_with_encoding_fallback(ftp: FTP, target: str) -> None:
        """CWD a path, retrying legacy Windows encodings in the same session."""
        original_encoding = ftp.encoding
        tried: set[str] = set()
        last_perm: error_perm | None = None
        try:
            for encoding in (original_encoding, *_FTP_CONTROL_ENCODINGS):
                if encoding in tried:
                    continue
                tried.add(encoding)
                ftp.encoding = encoding
                try:
                    ftp.cwd(target)
                    return
                except error_perm as exc:
                    last_perm = exc
                    if target.isascii():
                        break
                    continue
            if last_perm is not None:
                raise last_perm
        finally:
            ftp.encoding = original_encoding

    @staticmethod
    def _safe_nlst(ftp: FTP) -> None:
        """Run NLST without failing a valid target due to legacy filename encoding."""
        original_encoding = ftp.encoding
        tried: set[str] = set()
        try:
            for encoding in (original_encoding, *_FTP_LISTING_ENCODINGS):
                if encoding in tried:
                    continue
                tried.add(encoding)
                ftp.encoding = encoding
                try:
                    ftp.nlst()
                    return
                except UnicodeDecodeError:
                    # The data stream failed to decode; drain the pending 226
                    # response when possible before trying another data encoding.
                    try:
                        ftp.voidresp()
                    except Exception:
                        pass
            # CWD already proved the directory is reachable; the monitor does not
            # use filenames, so a non-decodable listing must not become DOWN.
            return
        finally:
            ftp.encoding = original_encoding

    @staticmethod
    def _write_probe(ftp: FTP, cfg: ConnectionConfig) -> TargetResult:
        """Optional ≤1 KB write probe, always deleted (best effort) — RF-2."""
        directory = cfg.targets[0] if cfg.targets else "/"
        label = f"{directory.rstrip('/')}/{PROBE_NAME} (escritura)"
        payload = BytesIO(f"stability-monitor probe {to_iso(utc_now())}\n".encode())
        try:
            FtpChecker._cwd_with_encoding_fallback(ftp, directory)
            ftp.storbinary(f"STOR {PROBE_NAME}", payload)
        except error_perm as exc:
            return TargetResult(
                target=label,
                ok=False,
                error_type=ErrorType.PERMISSION,
                message=f"sin permiso de escritura: {exc}",
            )
        finally:
            try:
                ftp.delete(PROBE_NAME)
            except Exception:
                pass
        return TargetResult(target=label, ok=True)
