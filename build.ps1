# build.ps1 — Modo A: produce dist\StabilityMonitor\ autocontenido (PyInstaller onedir).
# Se ejecuta en la máquina de DESARROLLO (con internet). El resultado se copia
# por USB o red interna a la máquina destino, que no necesita Python.
#
# Requisitos: Windows x64 con Python 3.12 instalado (py -3.12 disponible).
# Uso:  powershell -ExecutionPolicy Bypass -File .\build.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "== StabilityMonitor build (Modo A) ==" -ForegroundColor Cyan

# 1. Entorno virtual de build
if (-not (Test-Path ".venv-build")) {
    py -3.12 -m venv .venv-build
}
$py = ".\.venv-build\Scripts\python.exe"
& $py -m pip install --upgrade pip --quiet
& $py -m pip install -r requirements.txt --quiet
& $py -m pip install pyinstaller==6.11.1 --quiet

# 2. Tests antes de empaquetar
& $py -m pip install -r requirements-dev.txt --quiet
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
