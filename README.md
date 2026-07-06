# StabilityMonitor

Monitor de disponibilidad **de bajo impacto** para servidores de archivos
(FTP, FTPS, SFTP, WebDAV/S) y bases de datos (PostgreSQL, MySQL, MariaDB,
SQL Server, Oracle), con historial de conectividad, incidentes, alertas y
reportes de estabilidad para clientes.

Se despliega como **ejecutable portable para Windows 10 Pro x64, 100 % offline**:
un `dist/` copiable por USB que no requiere instalar Python ni nada en la
máquina destino, sin ninguna dependencia de internet en tiempo de ejecución.

## Capacidades

- **Checkers** de FTP/FTPS/SFTP/WebDAV(S) y PostgreSQL/MySQL/MariaDB/SQL Server/
  Oracle (drivers en Python puro), con verificación de rutas y tablas concretas.
- **Política de cortesía** (el monitor nunca sobrecarga lo que vigila): una sola
  sesión por host, espaciado, jitter, rate limit y backoff durante caídas.
- **Incidentes y alertas**: máquina de estados con histéresis, una alerta por
  incidente (toast nativa + sonido + ícono de bandeja en Windows; SMTP/webhook
  opcionales hacia la LAN), recuperación con duración.
- **Dashboard local** (FastAPI en `127.0.0.1:8090`): CRUD de conexiones, probar
  conexión, estado en vivo, gráficas de latencia y disponibilidad.
- **Reportes** HTML autocontenidos por cliente + export CSV.
- **Modo demo** (`--demo`) con 30 días de historial sintético.

## Desarrollo

El build se hace en una máquina con internet (aquí, cualquier SO con Python
3.12); el artefacto final se traslada a Windows. Para desarrollar y probar:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest            # suite unitaria (sin servidores)
MONITOR_IT=1 .venv/bin/python -m pytest tests/integration   # requiere contenedores (ver docstring)
```

### Levantar el dashboard en desarrollo

```bash
python -m app.main            # http://127.0.0.1:8090
python -m app.main --demo     # con datos ficticios de 30 días
```

### Probar un chequeo puntual (CLI)

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

## Empaquetado y despliegue (Windows offline)

En la máquina de desarrollo (con internet):

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1   # → dist\StabilityMonitor\ (PyInstaller onedir)
```

En la máquina Windows destino (sin internet, sin admin):

```powershell
# tras copiar dist\StabilityMonitor\ por USB, dentro de esa carpeta:
powershell -ExecutionPolicy Bypass -File .\install.ps1  # autoarranque de usuario + inicia la app
```

El dashboard queda en `http://127.0.0.1:8090` y el ícono aparece en la bandeja.
Guía completa en [docs/USER_GUIDE.md](docs/USER_GUIDE.md).

## Documentación

- [docs/USER_GUIDE.md](docs/USER_GUIDE.md) — manual de usuario en español.
- [docs/DECISIONS.md](docs/DECISIONS.md) — decisiones de diseño.
- [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) — criterios de aceptación.
