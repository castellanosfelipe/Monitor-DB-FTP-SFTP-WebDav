"""Connection validation and model round-trip tests."""
from __future__ import annotations

from app.db import Database
from app.models import (
    DEFAULT_PORTS,
    ConnectionConfig,
    Protocol,
    validate_connection,
)


def make_cfg(**overrides) -> ConnectionConfig:
    base = dict(
        id=None, name="c1", client="ACME", protocol=Protocol.FTP,
        host="ftp.acme.local", port=21,
    )
    base.update(overrides)
    return ConnectionConfig(**base)


def test_default_ports_cover_every_protocol():
    assert set(DEFAULT_PORTS) == set(Protocol)
    assert DEFAULT_PORTS[Protocol.SFTP] == 22
    assert DEFAULT_PORTS[Protocol.ORACLE] == 1521


def test_valid_config_passes():
    assert validate_connection(make_cfg()) == []


def test_basic_field_validation():
    assert validate_connection(make_cfg(name="  "))
    assert validate_connection(make_cfg(host=""))
    assert validate_connection(make_cfg(host="ftp://x"))
    assert validate_connection(make_cfg(port=0))
    assert validate_connection(make_cfg(port=70000))
    assert validate_connection(make_cfg(interval_s=5))     # < 30 s
    assert validate_connection(make_cfg(interval_s=7200))  # > 1 h
    assert validate_connection(make_cfg(retries=99))
    assert validate_connection(make_cfg(degraded_ms=-5))
    assert validate_connection(make_cfg(ssl_mode="maybe"))


def test_file_targets_must_be_absolute():
    assert validate_connection(make_cfg(targets=["/clientes/acme"])) == []
    assert validate_connection(make_cfg(targets=["clientes/acme"]))


def test_db_targets_schema_or_table():
    cfg = make_cfg(protocol=Protocol.MYSQL, port=3306, targets=["ventas", "ventas.pedidos"])
    assert validate_connection(cfg) == []
    bad = make_cfg(protocol=Protocol.MYSQL, port=3306, targets=["ventas..pedidos"])
    assert validate_connection(bad)
    bad2 = make_cfg(protocol=Protocol.MYSQL, port=3306, targets=["ventas; drop"])
    assert validate_connection(bad2)


def test_key_auth_only_for_sftp_and_requires_path():
    assert validate_connection(make_cfg(auth_type="key"))  # FTP + key → error
    sftp_no_path = make_cfg(protocol=Protocol.SFTP, port=22, auth_type="key")
    assert validate_connection(sftp_no_path)
    sftp_ok = make_cfg(
        protocol=Protocol.SFTP, port=22, auth_type="key", key_path="/keys/id_ed25519"
    )
    assert validate_connection(sftp_ok) == []


def test_health_query_only_for_databases():
    assert validate_connection(make_cfg(health_query="SELECT 1"))  # FTP → error
    pg = make_cfg(
        protocol=Protocol.POSTGRES, port=5432, db_name="ventas", health_query="SELECT 1"
    )
    assert validate_connection(pg) == []


def test_postgres_and_oracle_require_db_name():
    assert validate_connection(make_cfg(protocol=Protocol.POSTGRES, port=5432))
    assert validate_connection(make_cfg(protocol=Protocol.ORACLE, port=1521))
    assert validate_connection(
        make_cfg(protocol=Protocol.SQLSERVER, port=1433)  # optional here
    ) == []


def test_write_check_only_for_file_protocols():
    assert validate_connection(
        make_cfg(protocol=Protocol.MYSQL, port=3306, write_check=True)
    )
    assert validate_connection(make_cfg(write_check=True)) == []


def test_connection_roundtrip_through_sqlite(tmp_path):
    db = Database(tmp_path / "t.db")
    cfg = make_cfg(
        protocol=Protocol.SFTP, port=2222, auth_type="key", key_path="/k/id",
        targets=["/in", "/out"], degraded_ms=800, notes="nota ñ", enabled=False,
    )
    connection_id = db.create_connection(cfg)
    loaded = db.get_connection(connection_id)
    assert loaded is not None
    assert loaded.protocol is Protocol.SFTP
    assert loaded.port == 2222
    assert loaded.targets == ["/in", "/out"]
    assert loaded.degraded_ms == 800
    assert loaded.enabled is False
    assert loaded.notes == "nota ñ"
    assert loaded.created_at and loaded.updated_at

    loaded.port = 22
    db.update_connection(loaded)
    again = db.get_connection(connection_id)
    assert again is not None and again.port == 22

    db.delete_connection(connection_id)
    assert db.get_connection(connection_id) is None
