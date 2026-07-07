"""FTP checker behavior for legacy Windows/IIS servers."""
from __future__ import annotations

from app.checkers.ftp import FtpChecker
from app.models import ConnectionConfig, Protocol


def make_cfg(**overrides) -> ConnectionConfig:
    base = dict(
        id=None,
        name="ftp",
        client="ACME",
        protocol=Protocol.FTP,
        host="10.128.2.5",
        port=21,
        username="monitor",
        targets=["/FONVIVIENDA_CAVIS_UT"],
    )
    base.update(overrides)
    return ConnectionConfig(**base)


class LegacyListingFtp:
    def __init__(self) -> None:
        self.encoding = "utf-8"
        self.cwd_target = ""
        self.nlst_encodings: list[str] = []
        self.voidresp_calls = 0

    def cwd(self, target: str) -> None:
        self.cwd_target = target

    def nlst(self):
        self.nlst_encodings.append(self.encoding)
        if self.encoding == "utf-8":
            raise UnicodeDecodeError("utf-8", b"\xd1", 0, 1, "invalid continuation byte")
        return ["CARPETA_CON_NOMBRE_LEGADO"]

    def voidresp(self) -> None:
        self.voidresp_calls += 1


def test_ftp_listing_unicode_decode_error_does_not_mark_target_down():
    ftp = LegacyListingFtp()

    result = FtpChecker._check_target(ftp, "/FONVIVIENDA_CAVIS_UT")

    assert result.ok is True
    assert ftp.cwd_target == "/FONVIVIENDA_CAVIS_UT"
    assert ftp.nlst_encodings[:2] == ["utf-8", "cp1252"]
    assert ftp.voidresp_calls == 1
    assert ftp.encoding == "utf-8"


class ConnectFtp:
    def __init__(self, encoding: str) -> None:
        self.encoding = encoding
        self.closed = False
        self.logged_in = False

    def connect(self, host: str, port: int, timeout: float) -> None:
        if self.encoding == "utf-8":
            raise UnicodeDecodeError("utf-8", b"\xd1", 0, 1, "invalid continuation byte")

    def login(self, username: str, password: str) -> None:
        self.logged_in = True

    def close(self) -> None:
        self.closed = True


def test_ftp_connect_retries_with_legacy_control_encoding(monkeypatch):
    created: list[ConnectFtp] = []

    def factory(cfg, encoding: str):
        ftp = ConnectFtp(encoding)
        created.append(ftp)
        return ftp

    monkeypatch.setattr(FtpChecker, "_new_client", staticmethod(factory))

    connected = FtpChecker()._connect(make_cfg(), "secret")

    assert [ftp.encoding for ftp in created] == ["utf-8", "cp1252"]
    assert created[0].closed is True
    assert connected.encoding == "cp1252"
    assert connected.logged_in is True
