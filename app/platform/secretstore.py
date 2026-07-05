"""Secret storage facade.

Tokens are prefixed with their scheme (``dpapi:`` / ``fernet:``) so that a
database moved between modes fails with a clear, actionable message instead of
a cryptic crypto error — DPAPI and Fernet are intentionally not interchangeable.
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
    from app.platform.secrets_fernet import FernetSecretStore

    return FernetSecretStore.from_environment(allow_keyfile=(mode == "dev"))
