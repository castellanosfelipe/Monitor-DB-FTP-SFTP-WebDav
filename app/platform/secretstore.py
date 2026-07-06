"""Secret storage facade.

En Windows (el objetivo de despliegue) los secretos se cifran con **DPAPI**,
ligados a la máquina y usuario, de modo que copiar ``monitor.db`` a otra
máquina no expone las credenciales. En la máquina de desarrollo/CI (no-Windows)
DPAPI no existe, así que se usa **Fernet** con un keyfile local — solo para
poder correr y probar la app fuera de Windows.

Los tokens llevan prefijo de esquema (``dpapi:`` / ``fernet:``) para que una
base movida entre entornos falle con un mensaje accionable en vez de un error
criptográfico críptico.
"""
from __future__ import annotations

from typing import Protocol as TypingProtocol

from app.platform.detect import runtime_mode


class SecretStoreError(Exception):
    """User-facing secret storage problem (message in Spanish)."""


class SecretStore(TypingProtocol):
    def encrypt(self, plain: str) -> str: ...

    def decrypt(self, token: str) -> str: ...


def get_secret_store(mode: str | None = None) -> SecretStore:
    mode = mode or runtime_mode()
    if mode == "windows":
        from app.platform.secrets_dpapi import DpapiSecretStore

        return DpapiSecretStore()
    # dev/CI (no-Windows): Fernet con keyfile local generado automáticamente.
    from app.platform.secrets_fernet import FernetSecretStore

    return FernetSecretStore.from_environment(allow_keyfile=True)
