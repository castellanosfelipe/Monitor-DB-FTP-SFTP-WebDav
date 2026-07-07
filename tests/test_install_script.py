"""Installation script safeguards for long-running Windows deployments."""
from __future__ import annotations

from pathlib import Path


def test_install_task_is_hardened_for_24x7_runtime():
    script = Path("install.ps1").read_text(encoding="utf-8")

    assert "-RestartCount 999" in script
    assert "-RestartInterval (New-TimeSpan -Minutes 1)" in script
    assert "-MultipleInstances IgnoreNew" in script
    assert "[System.Security.Principal.WindowsIdentity]::GetCurrent().Name" in script
    assert "/healthz" in script
