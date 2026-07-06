"""Fernet-based secret store — backend de desarrollo/CI (no-Windows).

En la máquina destino (Windows) los secretos usan DPAPI; este backend existe
solo para poder correr y probar la app fuera de Windows, donde DPAPI no está
disponible. La clave se toma de ``MONITOR_SECRET_KEY`` si está definida; si no,
se genera y persiste automáticamente un keyfile local (``data/secret.key``).
"""
from __future__ import annotations

import os
import stat

from cryptography.fernet import Fernet, InvalidToken

from app import config
from app.platform.secretstore import SecretStoreError

_KEYFILE_NAME = "secret.key"


class FernetSecretStore:
    PREFIX = "fernet:"

    def __init__(self, key: str | bytes) -> None:
        if isinstance(key, str):
            key = key.encode()
        try:
            self._fernet = Fernet(key)
        except Exception as exc:
            raise SecretStoreError(
                "MONITOR_SECRET_KEY no es una clave Fernet válida; genera una con: "
                "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            ) from exc

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode()

    @classmethod
    def from_environment(cls, allow_keyfile: bool = False) -> "FernetSecretStore":
        key = os.environ.get("MONITOR_SECRET_KEY", "").strip()
        if key:
            return cls(key)
        if allow_keyfile:
            path = config.data_dir() / _KEYFILE_NAME
            if path.exists():
                return cls(path.read_text().strip())
            key = cls.generate_key()
            path.write_text(key + "\n")
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0o600
            return cls(key)
        raise SecretStoreError(
            "MONITOR_SECRET_KEY no está definida y no se permite keyfile local."
        )

    def encrypt(self, plain: str) -> str:
        return self.PREFIX + self._fernet.encrypt(plain.encode()).decode()

    def decrypt(self, token: str) -> str:
        if token.startswith("dpapi:"):
            raise SecretStoreError(
                "Este secreto fue cifrado con DPAPI (modo Windows) y no puede "
                "descifrarse aquí; vuelve a ingresar la credencial."
            )
        if not token.startswith(self.PREFIX):
            raise SecretStoreError("Formato de secreto no reconocido; vuelve a ingresar la credencial.")
        try:
            return self._fernet.decrypt(token[len(self.PREFIX):].encode()).decode()
        except InvalidToken as exc:
            raise SecretStoreError(
                "No se pudo descifrar el secreto: MONITOR_SECRET_KEY no coincide "
                "con la clave usada al guardarlo."
            ) from exc
