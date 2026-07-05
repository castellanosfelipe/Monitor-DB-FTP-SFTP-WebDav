"""DPAPI-based secret store (Modo A, Windows).

Uses ``CryptProtectData`` bound to the local machine+user: the encrypted blob
is useless on any other machine or account, which is exactly what we want for
a portable folder that might be copied around. ``win32crypt`` is imported
lazily so this module stays importable (and mockable) on non-Windows CI.
"""
from __future__ import annotations

import base64

from app.platform.secretstore import SecretStoreError

_DESCRIPTION = "StabilityMonitor"


def _win32crypt():
    try:
        import win32crypt  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - only hit on misconfigured hosts
        raise SecretStoreError(
            "DPAPI no está disponible (falta pywin32); este modo solo funciona en Windows."
        ) from exc
    return win32crypt


class DpapiSecretStore:
    PREFIX = "dpapi:"

    def encrypt(self, plain: str) -> str:
        blob = _win32crypt().CryptProtectData(plain.encode("utf-8"), _DESCRIPTION, None, None, None, 0)
        return self.PREFIX + base64.b64encode(blob).decode()

    def decrypt(self, token: str) -> str:
        if token.startswith("fernet:"):
            raise SecretStoreError(
                "Este secreto fue cifrado con Fernet (modo Docker) y no puede "
                "descifrarse aquí; vuelve a ingresar la credencial."
            )
        if not token.startswith(self.PREFIX):
            raise SecretStoreError("Formato de secreto no reconocido; vuelve a ingresar la credencial.")
        blob = base64.b64decode(token[len(self.PREFIX):])
        try:
            _description, data = _win32crypt().CryptUnprotectData(blob, None, None, None, 0)
        except SecretStoreError:
            raise
        except Exception as exc:
            raise SecretStoreError(
                "No se pudo descifrar con DPAPI: el secreto fue guardado en otra "
                "máquina o con otro usuario de Windows; vuelve a ingresar la credencial."
            ) from exc
        return data.decode("utf-8")
