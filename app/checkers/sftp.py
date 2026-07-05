"""SFTP checker (paramiko).

Per RF-2 the check is: SSH handshake → authentication (password, or private
key + passphrase) → ``stat()`` of each target → clean close.

Host keys use trust-on-first-use: the first key seen for a host is stored in
``data/known_hosts``; a later mismatch fails the check with a ``tls`` cause
(possible reinstall or spoofing) instead of silently accepting it.
"""
from __future__ import annotations

import posixpath
from pathlib import Path

import paramiko
from paramiko.ssh_exception import NoValidConnectionsError

from app import config
from app.checkers.base import BaseChecker
from app.errors import CheckError, ErrorType, classify_exception
from app.models import ConnectionConfig, TargetResult
from app.util import to_iso, utc_now

PROBE_NAME = ".monitor_probe"


class SftpChecker(BaseChecker):
    def _execute(self, cfg: ConnectionConfig, secret: str | None) -> list[TargetResult]:
        if cfg.auth_type == "key":
            if not cfg.key_path or not Path(cfg.key_path).exists():
                raise CheckError(
                    ErrorType.AUTH, f"no se encontró la llave privada: {cfg.key_path!r}"
                )

        client = paramiko.SSHClient()
        known_hosts = config.known_hosts_path()
        known_hosts.touch(exist_ok=True)
        client.load_host_keys(str(known_hosts))
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # TOFU

        try:
            client.connect(
                hostname=cfg.host,
                port=cfg.port,
                username=cfg.username or None,
                password=secret if cfg.auth_type == "password" else None,
                key_filename=cfg.key_path if cfg.auth_type == "key" else None,
                passphrase=secret if cfg.auth_type == "key" else None,
                timeout=cfg.timeout_s,
                banner_timeout=cfg.timeout_s,
                auth_timeout=cfg.timeout_s,
                allow_agent=False,
                look_for_keys=False,
            )
        except paramiko.BadHostKeyException as exc:
            raise CheckError(
                ErrorType.TLS,
                "la clave del host cambió respecto a la registrada "
                "(posible reinstalación del servidor o suplantación)",
            ) from exc
        except paramiko.PasswordRequiredException as exc:
            raise CheckError(ErrorType.AUTH, "la llave privada requiere passphrase") from exc
        except paramiko.AuthenticationException as exc:
            raise CheckError(ErrorType.AUTH, f"autenticación rechazada: {exc}") from exc
        except NoValidConnectionsError as exc:
            raise CheckError(ErrorType.TCP_CONNECT, f"no se pudo conectar: {exc}") from exc
        except paramiko.SSHException as exc:
            raise CheckError(ErrorType.PROTOCOL, f"error SSH: {exc}") from exc

        try:
            sftp = client.open_sftp()
            channel = sftp.get_channel()
            if channel is not None:
                channel.settimeout(cfg.timeout_s)
            results = [self._check_target(sftp, target) for target in cfg.targets]
            if cfg.write_check:
                results.append(self._write_probe(sftp, cfg))
            return results
        finally:
            client.close()

    @staticmethod
    def _check_target(sftp: paramiko.SFTPClient, target: str) -> TargetResult:
        try:
            sftp.stat(target)
        except FileNotFoundError:
            return TargetResult(
                target=target,
                ok=False,
                error_type=ErrorType.TARGET_MISSING,
                message="la ruta no existe",
            )
        except PermissionError:
            return TargetResult(
                target=target,
                ok=False,
                error_type=ErrorType.PERMISSION,
                message="permiso denegado",
            )
        except OSError as exc:
            error_type, message = classify_exception(exc)
            return TargetResult(target=target, ok=False, error_type=error_type, message=message)
        return TargetResult(target=target, ok=True)

    @staticmethod
    def _write_probe(sftp: paramiko.SFTPClient, cfg: ConnectionConfig) -> TargetResult:
        """Optional ≤1 KB write probe, always deleted (best effort) — RF-2."""
        directory = cfg.targets[0] if cfg.targets else "."
        path = posixpath.join(directory, PROBE_NAME)
        label = f"{path} (escritura)"
        try:
            with sftp.open(path, "w") as handle:
                handle.write(f"stability-monitor probe {to_iso(utc_now())}\n")
        except PermissionError as exc:
            return TargetResult(
                target=label,
                ok=False,
                error_type=ErrorType.PERMISSION,
                message=f"sin permiso de escritura: {exc}",
            )
        except OSError as exc:
            error_type, message = classify_exception(exc)
            return TargetResult(target=label, ok=False, error_type=error_type, message=message)
        finally:
            try:
                sftp.remove(path)
            except Exception:
                pass
        return TargetResult(target=label, ok=True)
