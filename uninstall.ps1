# uninstall.ps1 — Modo A: quita el autoarranque y detiene la aplicación.
# Los datos (data\, logs\, reports\) NO se borran: elimínalos a mano si quieres
# deshacerte también del historial y los secretos cifrados.
#   powershell -ExecutionPolicy Bypass -File .\uninstall.ps1

$ErrorActionPreference = "SilentlyContinue"
$taskName = "StabilityMonitor"

Stop-ScheduledTask -TaskName $taskName
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
Get-Process StabilityMonitor -ErrorAction SilentlyContinue | Stop-Process -Force

Write-Host "Autoarranque eliminado y aplicación detenida." -ForegroundColor Green
Write-Host "La carpeta con datos e historial queda intacta; bórrala manualmente si ya no la necesitas."
