"""Runtime mode detection (Modo A).

Modes:
- ``windows``: Windows portable, offline (DPAPI, bandeja/toasts). El objetivo.
- ``dev``:     máquina de desarrollo/CI no-Windows (build y tests). Se comporta
  como Windows salvo por el almacén de secretos (Fernet con keyfile local, ya
  que DPAPI solo existe en Windows).

Forzable con ``MONITOR_MODE=windows`` (útil para probar la ruta DPAPI en CI con
mocks). Fuera de eso, la plataforma decide.
"""
from __future__ import annotations

import os
import sys


def runtime_mode() -> str:
    if os.environ.get("MONITOR_MODE", "").strip().lower() == "windows":
        return "windows"
    if sys.platform == "win32":
        return "windows"
    return "dev"
