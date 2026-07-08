"""Launcher regression tests for frozen Windows builds."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import launcher


def test_launcher_repairs_missing_standard_streams(monkeypatch):
    monkeypatch.setattr(sys, "stdin", None)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    launcher._ensure_standard_streams()

    assert sys.stdin is not None
    assert sys.stdout is not None
    assert sys.stderr is not None
    assert isinstance(sys.stderr.isatty(), bool)


def test_launcher_self_test_imports_oracle_crypto_dependencies():
    assert launcher._run_self_test() == 0


def test_main_disables_uvicorn_default_logging_config(monkeypatch):
    from app import main as main_module

    ctx = SimpleNamespace(engine=None, notifier=None)
    app = SimpleNamespace(state=SimpleNamespace(ctx=ctx))
    captured: dict[str, object] = {}

    monkeypatch.setattr(sys, "argv", ["launcher.py"])
    monkeypatch.delenv("MONITOR_BIND_LAN", raising=False)
    monkeypatch.delenv("MONITOR_DEMO", raising=False)
    monkeypatch.delenv("MONITOR_PORT", raising=False)
    monkeypatch.setattr(main_module, "runtime_mode", lambda: "dev")
    monkeypatch.setattr(main_module, "setup_logging", lambda mode: None)
    monkeypatch.setattr(main_module, "build_context", lambda mode: ctx)
    monkeypatch.setattr(main_module, "create_app", lambda context: app)
    monkeypatch.setattr(
        main_module.uvicorn,
        "run",
        lambda *args, **kwargs: captured.update({"args": args, "kwargs": kwargs}),
    )

    main_module.main()

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["log_config"] is None
