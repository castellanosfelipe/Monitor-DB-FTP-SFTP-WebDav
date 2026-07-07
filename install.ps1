# install.ps1 - Modo A: autoarranque a nivel de USUARIO (sin administrador).
# Ejecutar DENTRO de la carpeta StabilityMonitor copiada a la maquina destino:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#
# Crea una tarea programada que arranca StabilityMonitor.exe al iniciar sesion
# del usuario actual, lo reinicia si cae, evita instancias duplicadas y lo
# inicia ahora mismo. No toca HKLM ni Program Files.

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$exe = Join-Path $here "StabilityMonitor.exe"
$taskName = "StabilityMonitor"
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$port = if ($env:MONITOR_PORT) { $env:MONITOR_PORT } else { "8090" }
$healthUrl = "http://127.0.0.1:$port/healthz"

if (-not (Test-Path $exe)) {
    throw "No se encontro StabilityMonitor.exe en $here. Ejecuta este script dentro de la carpeta copiada."
}

# Tarea programada de usuario: al iniciar sesion, sin privilegios elevados.
# RestartCount alto para operacion 24/7; MultipleInstances evita duplicados
# si el usuario ejecuta install.ps1 mas de una vez o hay reintentos solapados.
$action = New-ScheduledTaskAction -Execute $exe -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)   # sin limite: corre siempre

Register-ScheduledTask -TaskName $taskName `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Monitor de estabilidad de servidores (arranque de usuario)" `
    -Force | Out-Null

Write-Host "Tarea programada '$taskName' registrada para $currentUser." -ForegroundColor Green
Write-Host "Reinicio automatico: hasta 999 intentos, cada 1 minuto; sin instancias duplicadas."

# Arrancar ahora.
Start-ScheduledTask -TaskName $taskName

$ready = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 1
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $healthUrl -TimeoutSec 2
        if ($response.StatusCode -eq 200) {
            $ready = $true
            break
        }
    } catch {
        # Aun arrancando.
    }
}

if ($ready) {
    Write-Host "StabilityMonitor iniciado. Dashboard: http://127.0.0.1:$port" -ForegroundColor Green
    Write-Host "El icono aparece en la bandeja del sistema (junto al reloj)."
} else {
    Write-Warning "La tarea fue registrada, pero /healthz no respondio en $healthUrl. Revisa logs\app.log."
}

Write-Host "Nota 24/7: manten la sesion de Windows iniciada y desactiva suspension/hibernacion del equipo."
