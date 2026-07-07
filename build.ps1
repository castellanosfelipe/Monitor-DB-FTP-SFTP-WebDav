# build.ps1 — Modo A: produce dist\StabilityMonitor\ autocontenido (PyInstaller onedir).
#
# 100% offline: todas las dependencias (runtime, dev y PyInstaller) están
# vendorizadas en .\wheelhouse\ como wheels win_amd64/cp312 — este script
# NUNCA toca PyPI ni internet. Basta con tener Python 3.12 instalado
# (instalador oficial también incluido en .\vendor\, ver docs/USER_GUIDE.md).
# El resultado (dist\StabilityMonitor\) se copia por USB o red interna a la
# máquina destino, que no necesita Python.
#
# Requisitos: Windows x64 con Python 3.12 instalado (py -3.12 disponible).
# Uso:  powershell -ExecutionPolicy Bypass -File .\build.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "== StabilityMonitor build (Modo A) — 100% offline ==" -ForegroundColor Cyan

if (-not (Test-Path ".\wheelhouse")) {
    throw "No se encontró .\wheelhouse\ (dependencias vendorizadas). " +
          "Este script está pensado para correr sin internet; si el " +
          "wheelhouse no vino con el repo, no se puede continuar sin red."
}

# 1. Entorno virtual de build
#    Preferimos el launcher `py -3.12` (máquina con varias versiones de Python);
#    si no está, caemos a `python` (p. ej. el runner de CI con Python 3.12 ya
#    en el PATH). Ambos caminos son offline.
if (-not (Test-Path ".venv-build")) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        py -3.12 -m venv .venv-build
    } else {
        python -m venv .venv-build
    }
    if ($LASTEXITCODE -ne 0) { throw "No se pudo crear el entorno virtual con Python 3.12." }
}
$py = ".\.venv-build\Scripts\python.exe"
$pipOffline = @("--no-index", "--find-links", ".\wheelhouse")

# El pip que trae el venv (vía ensurepip) basta para instalar desde el
# wheelhouse local; no hace falta actualizarlo ni tocar la red.
& $py -m pip install @pipOffline -r requirements.txt --quiet
& $py -m pip install @pipOffline pyinstaller==6.11.1 --quiet

# 2. Tests antes de empaquetar
& $py -m pip install @pipOffline -r requirements-dev.txt --quiet
& $py -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "Los tests fallaron; no se genera el paquete." }

# 3. PyInstaller onedir
#    --noconsole: app de bandeja; el diagnóstico va a logs\app.log
#    Los imports perezosos (DPAPI, toasts, bandeja) se declaran como hidden.
& $py -m PyInstaller launcher.py `
    --name StabilityMonitor `
    --onedir `
    --noconsole `
    --noconfirm `
    --clean `
    --add-data "static;static" `
    --add-data "templates;templates" `
    --hidden-import win32crypt `
    --hidden-import winotify `
    --hidden-import winsound `
    --hidden-import pystray `
    --hidden-import "pystray._win32" `
    --hidden-import "PIL.Image" `
    --hidden-import "PIL.ImageDraw" `
    --collect-submodules apscheduler `
    --collect-submodules oracledb

if ($LASTEXITCODE -ne 0) { throw "PyInstaller falló." }

# 4. Scripts de instalación junto al ejecutable
Copy-Item install.ps1, uninstall.ps1 -Destination "dist\StabilityMonitor\"

Write-Host ""
Write-Host "Listo: dist\StabilityMonitor\" -ForegroundColor Green
Write-Host "Copia esa carpeta completa a la máquina destino y ejecuta install.ps1 allí."
