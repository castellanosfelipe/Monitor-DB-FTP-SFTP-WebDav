"""Checker registry: maps each protocol to its checker implementation."""
from __future__ import annotations

from app.checkers.base import BaseChecker
from app.checkers.ftp import FtpChecker
from app.checkers.sftp import SftpChecker
from app.checkers.webdav import WebDavChecker
from app.models import Protocol

_REGISTRY: dict[Protocol, type[BaseChecker]] = {
    Protocol.FTP: FtpChecker,
    Protocol.FTPS: FtpChecker,
    Protocol.SFTP: SftpChecker,
    Protocol.WEBDAV: WebDavChecker,
    Protocol.WEBDAVS: WebDavChecker,
}


def get_checker(protocol: Protocol) -> BaseChecker:
    """Return a fresh checker instance (checkers hold no shared mutable state)."""
    cls = _REGISTRY.get(protocol)
    if cls is None:
        raise NotImplementedError(
            f"El checker para {protocol.value} llega en la Fase 2 (bases de datos)."
        )
    return cls()
