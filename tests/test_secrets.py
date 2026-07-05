"""Secret store tests: Fernet round-trip, DPAPI via mock, cross-mode failures."""
from __future__ import annotations

import sys
import types

import pytest

from app.platform.secrets_dpapi import DpapiSecretStore
from app.platform.secrets_fernet import FernetSecretStore
from app.platform.secretstore import SecretStoreError


@pytest.fixture()
def fake_win32crypt(monkeypatch):
    """Reversible stand-in for DPAPI so the suite runs on non-Windows CI."""

    def protect(data, description, entropy, reserved, prompt, flags):
        return b"DPAPI!" + bytes(reversed(data))

    def unprotect(blob, entropy, reserved, prompt, flags):
        assert blob.startswith(b"DPAPI!")
        return ("desc", bytes(reversed(blob[len(b"DPAPI!"):])))

    module = types.SimpleNamespace(CryptProtectData=protect, CryptUnprotectData=unprotect)
    monkeypatch.setitem(sys.modules, "win32crypt", module)
    return module


def test_fernet_roundtrip_and_opacity():
    store = FernetSecretStore(FernetSecretStore.generate_key())
    token = store.encrypt("hunter2-ñ-密码")
    assert token.startswith("fernet:")
    assert "hunter2" not in token  # never readable in monitor.db
    assert store.decrypt(token) == "hunter2-ñ-密码"


def test_fernet_wrong_key_fails_clearly():
    token = FernetSecretStore(FernetSecretStore.generate_key()).encrypt("secreto")
    other = FernetSecretStore(FernetSecretStore.generate_key())
    with pytest.raises(SecretStoreError, match="MONITOR_SECRET_KEY"):
        other.decrypt(token)


def test_fernet_invalid_key_rejected():
    with pytest.raises(SecretStoreError):
        FernetSecretStore("not-a-valid-key")


def test_fernet_from_environment(monkeypatch):
    monkeypatch.setenv("MONITOR_SECRET_KEY", FernetSecretStore.generate_key())
    store = FernetSecretStore.from_environment()
    assert store.decrypt(store.encrypt("x")) == "x"


def test_fernet_requires_key_when_keyfile_not_allowed(monkeypatch):
    monkeypatch.delenv("MONITOR_SECRET_KEY", raising=False)
    with pytest.raises(SecretStoreError, match="MONITOR_SECRET_KEY"):
        FernetSecretStore.from_environment(allow_keyfile=False)


def test_fernet_dev_keyfile_is_created_and_reused(monkeypatch, tmp_path):
    monkeypatch.delenv("MONITOR_SECRET_KEY", raising=False)
    monkeypatch.setenv("MONITOR_DATA_DIR", str(tmp_path))
    first = FernetSecretStore.from_environment(allow_keyfile=True)
    token = first.encrypt("persistente")
    keyfile = tmp_path / "data" / "secret.key"
    assert keyfile.exists()
    second = FernetSecretStore.from_environment(allow_keyfile=True)
    assert second.decrypt(token) == "persistente"


def test_dpapi_roundtrip(fake_win32crypt):
    store = DpapiSecretStore()
    token = store.encrypt("contraseña")
    assert token.startswith("dpapi:")
    assert "contraseña" not in token
    assert store.decrypt(token) == "contraseña"


def test_cross_mode_tokens_fail_with_actionable_message(fake_win32crypt):
    dpapi = DpapiSecretStore()
    fernet = FernetSecretStore(FernetSecretStore.generate_key())
    with pytest.raises(SecretStoreError, match="DPAPI"):
        fernet.decrypt(dpapi.encrypt("s"))
    with pytest.raises(SecretStoreError, match="Fernet"):
        dpapi.decrypt(fernet.encrypt("s"))
