"""Generate a Fernet key for ``MONITOR_SECRET_KEY`` (Modo B).

Usage: ``python -m app.keygen``. Store the key safely: without it, the
secrets saved in the database are unrecoverable by design.
"""
from app.platform.secrets_fernet import FernetSecretStore

if __name__ == "__main__":
    print(FernetSecretStore.generate_key())
