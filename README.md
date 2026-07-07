# StabilityMonitor

Monitor de disponibilidad **de bajo impacto** para servidores de archivos
(FTP, FTPS, SFTP, WebDAV/S) y bases de datos (PostgreSQL, MySQL, MariaDB,
SQL Server, Oracle), con historial de conectividad, incidentes, alertas y
reportes de estabilidad para clientes.

Se despliega como **ejecutable portable para Windows 10 Pro x64, 100 % offline**:
un `dist/` copiable por USB que no requiere instalar Python ni nada en la
máquina destino, sin ninguna dependencia de internet en tiempo de ejecución.

**El repositorio incluye todo lo necesario para construirlo sin internet**:
`wheelhouse/` trae las ~47 dependencias exactas (runtime + PyInstaller) como
wheels de Windows/cp312, y `vendor/` trae el instalador oficial de Python
3.12.10 para Windows. `build.ps1` instala exclusivamente desde `wheelhouse/`
y nunca contacta PyPI.

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

## Empaquetado y despliegue (Windows, 100 % offline)

**Opción recomendada — descargar el ejecutable ya construido.** Cada tag
`vX.Y.Z` dispara un workflow de GitHub Actions que compila el paquete en un
runner **Windows real** (PyInstaller no compila de forma cruzada) y publica el
ZIP en [Releases](https://github.com/castellanosfelipe/Monitor-DB-FTP-SFTP-WebDav/releases).
Descárgalo desde cualquier máquina con internet, cópialo por USB a la máquina
destino y ejecuta `install.ps1` — el destino nunca necesita internet ni Python.

**Opción B — construirlo tú mismo** (en una máquina Windows, también sin
internet gracias al wheelhouse y al instalador de Python vendorizados):

```powershell
# si Python 3.12 no está instalado, usa el instalador incluido:
vendor\python-3.12.10-amd64.exe
powershell -ExecutionPolicy Bypass -File .\build.ps1   # → dist\StabilityMonitor\ (PyInstaller onedir)
```

Instalación en la máquina destino (sin internet, sin admin), sea el ZIP de
Releases descomprimido o tu propio `dist\StabilityMonitor\`:

```powershell
# dentro de la carpeta StabilityMonitor\:
powershell -ExecutionPolicy Bypass -File .\install.ps1  # autoarranque de usuario + inicia la app
```

El dashboard queda en `http://127.0.0.1:8090` y el ícono aparece en la bandeja.
Guía completa en [docs/USER_GUIDE.md](docs/USER_GUIDE.md).

## Documentación

- [docs/USER_GUIDE.md](docs/USER_GUIDE.md) — manual de usuario en español.
- [docs/DECISIONS.md](docs/DECISIONS.md) — decisiones de diseño.
- [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) — criterios de aceptación.
