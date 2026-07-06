# StabilityMonitor

Monitor de disponibilidad **de bajo impacto** para servidores de archivos
(FTP, FTPS, SFTP, WebDAV/S) y bases de datos (PostgreSQL, MySQL, MariaDB,
SQL Server, Oracle), con historial de conectividad, incidentes, alertas y
reportes de estabilidad para clientes.

Dos modos con la misma base de código:

- **Modo A** — Windows 10 Pro x64, 100 % offline (ejecutable portable, PyInstaller).
- **Modo B** — Docker Compose (online, dashboard con HTTP Basic).

## Estado

| Fase | Contenido | Estado |
|---|---|---|
| 1 | Núcleo: modelo de datos, checkers FTP/FTPS/SFTP/WebDAV(S), clasificación de errores, política de cortesía, máquina de incidentes, CLI | ✅ |
| 2 | Checkers de bases de datos (PostgreSQL, MySQL, MariaDB, SQL Server, Oracle thin) probados contra contenedores reales | ✅ |
| 3 | Dashboard: FastAPI + scheduler, CRUD, probar conexión, estado en vivo, Basic Auth | ✅ |
| 4 | Alertas: toasts/bandeja/sonido en Windows, SMTP y webhook opcionales, recordatorios, purga nocturna, ajustes desde el dashboard | ✅ |
| 5 | Gráficas (latencia + timeline, Chart.js local), reportes HTML autocontenidos por cliente, export CSV | ✅ |
| 6 | Empaquetado (PyInstaller + autoarranque / Docker Compose), modo demo, backup/restore, manual de usuario | ✅ |

## Desarrollo

Requiere Python 3.12.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest            # suite unitaria (sin servidores)
MONITOR_IT=1 .venv/bin/python -m pytest tests/integration   # requiere contenedores (ver docstring)
```

### Probar un chequeo puntual (CLI de la Fase 1)

Contra una conexión guardada en `data/monitor.db`:

```bash
python -m app.check 1
```

O ad-hoc, sin base de datos, desde un JSON:

```bash
python -m app.check --file conn.json
```

```json
{
  "protocol": "SFTP",
  "host": "10.0.0.5",
  "username": "monitor",
  "secret": "...",
  "targets": ["/clientes/acme/entrada"],
  "timeout_s": 10
}
```

Códigos de salida: `0` UP · `1` DEGRADED · `2` DOWN · `3` configuración inválida.

### Levantar el dashboard en desarrollo

```bash
python -m app.main            # http://127.0.0.1:8090
python -m app.main --demo     # con datos ficticios de 30 días
```

## Despliegue

**Modo A (Windows offline)**: `build.ps1` en la máquina con internet →
copiar `dist\StabilityMonitor\` por USB → `install.ps1` en el destino
(autoarranque de usuario, sin admin). Detalle en el manual.

**Modo B (Docker)**:

```bash
cp .env.example .env
python -m app.keygen          # → MONITOR_SECRET_KEY en .env
docker compose up -d          # dashboard en :8090 con Basic Auth
```

## Documentación

- [docs/USER_GUIDE.md](docs/USER_GUIDE.md) — manual de usuario en español (ambos modos).
- [docs/DECISIONS.md](docs/DECISIONS.md) — decisiones de diseño por fase.
- [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) — pasada final contra los criterios de aceptación.
