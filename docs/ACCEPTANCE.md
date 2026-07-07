# Pasada final contra los criterios de aceptación

Estado al cierre de la Fase 6. «Verificado» = ejecutado en este entorno de
desarrollo (macOS + contenedores locales); lo específico de Windows se
verificó por diseño y tests unitarios y queda marcado para el smoke test en
la máquina Windows real.

| # | Criterio | Estado | Evidencia |
|---|---|---|---|
| 1 | Crear conexión SFTP con llave privada, probarla y verla monitoreada en <1 min | ✅ Verificado (flujo) | CRUD+probar por API en vivo (Fase 3); llave privada: `tests/test_models.py`, checker SFTP con `key_filename`+passphrase; `schedule_connection(immediate=True)` arranca en ~1 s |
| 2 | PostgreSQL: probar, monitorear; al detener la BD abre incidente con causa correcta y al levantarla cierra con duración | ✅ Verificado | Fase 3 en vivo contra postgres:16 (UP con latencia); máquina de incidentes: `tests/test_incidents.py`; causas reales: `tests/integration/` (35 tests, 5 motores) |
| 3 | Alerta de caída en ≤ intervalo×reintentos+timeout | ✅ Verificado | FTP fantasma en vivo: DOWN confirmado e incidente en 2 chequeos (retries=1); alerta inmediata al confirmar (`tests/test_alerts.py`) |
| 4 | Ruta virtual inexistente ⇒ causa `ruta/objeto` ≠ «servidor caído»; tabla inexistente ⇒ `objeto` ≠ «BD caída» | ✅ Verificado | Smoke WebDAV en vivo (404→`target_missing`, DEGRADED); Postgres en vivo: tabla fantasma → `target_missing`, BD inexistente → `db_missing` |
| 5 | Cortesía: 5 conexiones al mismo host serializadas y espaciadas, nunca 2 sesiones simultáneas | ✅ Verificado | `tests/test_throttle.py::test_same_host_checks_never_overlap_with_real_threads` (hilos reales, sin solapamiento, espaciado ≥) |
| 6 | Backoff progresivo hasta el tope y vuelta al intervalo normal | ✅ Verificado | `tests/test_throttle.py` (progresión ×2ⁿ, tope) + `tests/test_api.py::test_compute_next_delay_normal_and_backoff` (reset al recuperar); observado en vivo (espaciado creciente del FTP fantasma) |
| 7 | Query de salud que no empiece por SELECT rechazada al guardar y al ejecutar | ✅ Verificado | `tests/test_health_query.py` (guardar) + `tests/test_db_checkers.py::test_health_query_revalidated_at_execution` (nunca llega al driver) |
| 8 | Reporte mensual: HTML y PDF descargable, abren sin internet, gráficas correctas en HTML | ✅ Verificado | `tests/test_reports.py` (sin `src/href` remotos, 2 SVG, PDF real `%PDF`); generado en vivo y validado estructuralmente (viewBox, coordenadas) |
| 9 | Tras reiniciar Windows, arranca sola y sigue monitoreando | ⚠ Por diseño | Tarea programada ONLOGON de usuario (`install.ps1`), incidentes abiertos se recargan (`tests/test_incidents.py::test_tracker_recovers...`). **Smoke pendiente en Windows real** |
| 10 | `dist/` copiado por USB a Windows 10 limpio arranca con doble clic | ⚠ Por diseño | `build.ps1` (onedir autocontenido, hidden-imports declarados, tests previos al build). **Smoke pendiente en Windows real** |
| 11 | Credenciales no legibles en `monitor.db` (DPAPI, ligado a máquina/usuario de Windows) | ✅ Verificado | El secreto se guarda como token cifrado, nunca en claro; DPAPI probado con mock en CI (`tests/test_secrets.py`) y sin fuga en `error_msg`/logs |
| 12 | Sin tráfico distinto de los chequeos configurados | ✅ Por construcción + verificado parcial | Cero dependencias de red en runtime (Chart.js/fuentes locales, sin telemetría, sin CDN); tráfico identificable verificado en vivo (`pg_stat_activity` → `StabilityMonitor/0.1.0`, 0 sesiones remanentes). Inspección con Wireshark pendiente como parte del smoke en destino |
| 13 | Alias virtuales no crean conexiones ni sesiones adicionales y no alteran parámetros técnicos | ✅ Verificado | Alias persisten como `aliases_json` local; `tests/test_api.py::test_alias_only_update_does_not_reschedule_or_change_technical_params` confirma que editar alias no reprograma chequeos ni cambia protocolo, host, puerto, usuario, objetivos o secreto |
| 14 | Alias y caracteres acentuados sobreviven guardado, backup/restore, búsqueda y reportes | ✅ Verificado | `tests/test_models.py` valida Unicode NFC/duplicados/valores inseguros; `tests/test_demo_and_backup.py` preserva alias en JSON; `tests/test_reports.py` imprime alias en HTML/PDF |

## Smoke test recomendado en la máquina Windows destino

1. `build.ps1` en la máquina de desarrollo → copiar `dist\StabilityMonitor\` por USB.
2. `install.ps1` → verificar ícono de bandeja + dashboard en `127.0.0.1:8090`.
3. Crear una conexión real (SFTP con llave y una BD), «Probar conexión», dejar 10 min.
4. Apagar el servidor de prueba → toast + sonido + bandeja roja + incidente; encender → toast de recuperado con duración.
5. Reiniciar Windows → confirmar que reaparece solo (tarea `StabilityMonitor`).
6. Wireshark: filtrar por el host monitoreado; confirmar que solo hay tráfico de los chequeos y nada hacia internet.
