# install.ps1 — Modo A: autoarranque a nivel de USUARIO (sin administrador).
# Ejecutar DENTRO de la carpeta StabilityMonitor copiada a la máquina destino:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#
# Crea una tarea programada que arranca StabilityMonitor.exe al iniciar sesión
# del usuario actual, y lo inicia ahora mismo. No toca HKLM ni Program Files.

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$exe = Join-Path $here "StabilityMonitor.exe"
$taskName = "StabilityMonitor"

if (-not (Test-Path $exe)) {
    throw "No se encontró StabilityMonitor.exe en $here. Ejecuta este script dentro de la carpeta copiada."
}

# Tarea programada de usuario: al iniciar sesión, sin privilegios elevados.
$action = New-ScheduledTaskAction -Execute $exe -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)   # sin límite: corre siempre

Register-ScheduledTask -TaskName $taskName `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Monitor de estabilidad de servidores (arranque de usuario)" `
    -Force | Out-Null

Write-Host "Tarea programada '$taskName' registrada (se inicia al abrir sesión)." -ForegroundColor Green

# Arrancar ahora
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 3
Write-Host "StabilityMonitor iniciado. Dashboard: http://127.0.0.1:8090" -ForegroundColor Green
Write-Host "El ícono aparece en la bandeja del sistema (junto al reloj)."
