# Decisiones de diseño

Registro de decisiones no triviales. Cada entrada indica la fase en que se tomó.

## Fase 1 — Núcleo

### D-001 · Columna `database` renombrada a `db_name`
`DATABASE` es palabra clave de SQLite (`ATTACH DATABASE`). Renombrarla evita
tener que citarla en cada consulta para siempre. El resto del esquema sigue el
punto de partida de la especificación.

### D-002 · Taxonomía de errores ampliada
A la lista de la especificación se agregan:
- `tcp_connect` (conexión rechazada / host inalcanzable) separado de
  `tcp_timeout`: "el servicio no está escuchando" y "la red no responde" son
  diagnósticos distintos y ambos aparecen en reportes.
- `latency` para DEGRADED por umbral de latencia (permite distinguirlo de un
  objetivo fallido en la tabla `checks`).
- `unknown` como último recurso; nunca debería aparecer en operación normal.

### D-003 · Semántica de reintentos
`retries = N` significa: DOWN se confirma tras `N + 1` chequeos fallidos
**consecutivos y a la cadencia normal programada**. No hay re-chequeos
inmediatos tras un fallo: re-chequear al instante a un servidor que acaba de
fallar contradice la política de cortesía (RF-2). Consecuencia documentada: la
detección tarda como máximo `intervalo × (retries + 1) + timeout`, coherente
con el criterio de aceptación.

### D-004 · DEGRADED no abre incidentes
Literal de RF-3: un incidente abre solo con DOWN confirmado (no conecta o no
autentica). Un objetivo fallido (ruta/tabla inexistente) produce DEGRADED y
queda registrado con su causa en cada fila de `checks`, por lo que los
reportes sí pueden distinguir "la tabla del cliente desapareció" de "servidor
caído" sin generar incidentes de disponibilidad falsos.

### D-005 · `started_at` del incidente = primer fallo de la racha
El downtime reportado va desde el **primer** chequeo fallido de la racha (una
vez confirmada) hasta el primer éxito posterior. Es la interpretación más
honesta de la definición de uptime de RF-6: el servidor ya estaba caído en el
primer fallo, solo que aún no lo habíamos confirmado.

### D-006 · Latencia solo en chequeos exitosos
`checks.latency_ms` es NULL para DOWN (la especificación pide medir latencia
"de cada chequeo exitoso"; el tiempo-hasta-fallo mezcla timeouts configurados
con latencia real y contaminaría los promedios).

### D-007 · Tokens de secretos con prefijo de esquema
Los secretos se guardan como `dpapi:<b64>` o `fernet:<token>`. Una base movida
entre modos falla con un mensaje accionable ("reingresa la credencial") en vez
de un error criptográfico críptico. En modo Docker la clave `MONITOR_SECRET_KEY`
es **obligatoria** (sin fallback), cumpliendo el criterio de aceptación de que
sin ella los secretos son irrecuperables. Solo en desarrollo (ni Windows ni
Docker, modo implícito `dev` no forzable) se genera un keyfile local
`data/secret.key` (chmod 600) por comodidad.

### D-008 · Orden de adquisición en la política de cortesía
En `Throttle.slot()`: primero el lock por host (serialización), después la
espera de espaciado/rate limit (sosteniendo el lock del host, que es
exactamente lo que se quiere serializar) y **al final** el semáforo global.
Así un chequeo esperando cortesía de su host nunca ocupa un slot global que
otro host podría usar. El espaciado se mide fin→inicio (más estricto que
inicio→inicio).

### D-009 · Backoff nunca por debajo del intervalo base
`backoff_delay()` devuelve como mínimo el intervalo base aunque el tope
configurado sea menor: durante una caída solo se reduce la frecuencia, jamás
se aumenta. El exponente se cuenta desde la confirmación de DOWN y se resetea
con el primer éxito.

### D-010 · SFTP: host keys con TOFU en `data/known_hosts`
Primera conexión registra la clave del host; un cambio posterior falla el
chequeo con causa `tls` (posible reinstalación o suplantación) en lugar de
aceptarla en silencio. Alternativa descartada: `AutoAddPolicy` sin persistencia
(inseguro) o verificación estricta manual (fricción excesiva para una
herramienta de monitoreo en LAN).

### D-011 · `ssl_mode` reutilizado en FTPS/WebDAVS para verificación de certificados
En protocolos de archivos con TLS, `required` verifica la cadena de
certificados; `preferred` (default) y `disabled` cifran sin verificar. Los
certificados autofirmados son la norma en servidores LAN; exigir verificación
por defecto haría que casi toda instalación real empezara en DOWN por `tls`.

### D-012 · FTP: `CWD` valida el objetivo; `NLST` tolera 550 tras `CWD` exitoso
Varios servidores FTP responden `550` a `NLST` sobre un directorio **vacío**.
Como el `CWD` previo ya probó existencia y acceso, ese 550 se trata como
directorio vacío y el objetivo cuenta como OK.

### D-013 · WebDAV: Basic con fallback automático a Digest
Se intenta HTTP Basic; si el servidor responde 401 ofreciendo Digest, se
reintenta una única vez con Digest. Cubre IIS/Apache antiguos sin agregar
dependencias.

### D-014 · Validación de query de salud por lista de palabras prohibidas
Además de exigir que empiece por `SELECT` y sea una sola sentencia, se rechazan
palabras clave de escritura/DDL/control (`INTO`, `FOR UPDATE`, `EXEC`, etc.)
con límites de palabra — `SELECT * FROM updates` es válido, `... FOR UPDATE`
no. Se rechaza `WITH ... SELECT` (la especificación dice literalmente
"comenzar por SELECT"). La validación corre al guardar **y** justo antes de
ejecutar.

### D-015 · requirements dividido en runtime y dev
`requirements.txt` (runtime, se empaqueta) y `requirements-dev.txt` (pytest).
`uvicorn` sin extras: menos binarios que empaquetar con PyInstaller. Para el
build offline reproducible, `build.ps1` (Fase 6) usará `pip download` con estos
pins exactos.

### D-016 · Nota sobre `oracledb`
La especificación lo describe como "100 % Python puro"; en realidad
`python-oracledb` incluye una extensión compilada (Cython) aunque el modo thin
no requiera Instant Client. Hay wheels oficiales para Windows x64, así que
PyInstaller lo empaqueta sin fricción. Validado en Fase 2 contra Oracle Free 23
en modo thin; el empaquetado se verifica en la Fase 6.

## Fase 2 — Checkers de bases de datos

### D-017 · Semántica de `ssl_mode` en bases de datos
Sigue la convención del ecosistema de cada motor (estilo libpq):
- `disabled` → sin TLS.
- `preferred` → comportamiento por defecto del driver. En la práctica, para
  pg8000 y PyMySQL equivale a sin TLS (no negocian TLS oportunista); en SQL
  Server el cifrado lo negocia el propio protocolo TDS según el servidor.
- `required` → fuerza TLS **sin verificación de cadena** (igual que
  `sslmode=require` de psql). Postgres: `ssl_context` sin verificación;
  MySQL/MariaDB: `ssl={}`; Oracle: protocolo `tcps`. SQL Server: no aplica
  (negociación TDS), documentado en el propio checker.
La verificación completa de certificados (verify-ca/verify-full) queda fuera
de alcance; en protocolos de archivos `required` sí verifica (D-011) porque
ahí no existe un nivel adicional.

### D-018 · Timeout de la query de salud: mecanismos por driver
El tope de 5 s se aplica con el mejor mecanismo disponible en cada driver:
Oracle usa la API pública `Connection.call_timeout`; pg8000 ajusta
temporalmente el timeout del socket (`_usock`); PyMySQL ajusta
`_read_timeout` (consultado por consulta). En SQL Server aplica el timeout de
socket de la sesión (`timeout` de pytds, = timeout de la conexión). En todos
los casos el timeout de conexión (`timeout_s`) actúa como límite exterior, y
un timeout durante la query de salud se reporta como `query_timeout` (no como
`tcp_timeout`).

### D-019 · Fallo de la query de salud ⇒ DEGRADED, no DOWN
Si la query de salud es rechazada por el validador, falla o excede su tiempo,
la conexión queda DEGRADED con la causa correspondiente: el servidor conectó y
autenticó; lo que falla es el contenido. DOWN queda reservado para "no conecta
o no autentica" (RF-2). La query rechazada jamás llega al driver (validación
también en tiempo de ejecución, verificada por test).

### D-020 · BD inexistente con usuario limitado en MySQL/MariaDB
Con un usuario de monitoreo sin privilegios globales, MySQL responde error
1044 («access denied») en lugar de 1049 («unknown database») para no revelar
si la base existe. Se clasifica como `permission`, distinto de `db_missing`,
pero ambos claramente distintos de "servidor caído". Verificado contra
contenedores reales; los tests de integración aceptan ambas causas para estos
motores.

### D-021 · Clasificación por mensaje en pytds y oracledb thin
- pytds reporta fallos de login a veces como `LoginError` y a veces como
  `OperationalError` plano según la ruta de código; la clasificación se hace
  por mensaje sobre toda la familia («login failed» → `auth`, «cannot open
  database» → `db_missing`). Descubierto contra el servidor real.
- oracledb thin envuelve la causa raíz en el texto de `DPY-6xxx`
  (p. ej. «Similar to ORA-12514»); los códigos `DPY-6xxx` se clasifican
  inspeccionando el mensaje (service desconocido → `db_missing`).

### D-022 · Entorno de integración local
Los tests de integración (`tests/integration/`, activados con `MONITOR_IT=1`)
corren contra contenedores locales: postgres:16-alpine, mysql:8.4, mariadb:11,
gvenzl/oracle-free:23-slim-faststart y, para SQL Server en máquinas ARM donde
la imagen oficial x64 no arranca bajo emulación, `azure-sql-edge` (ARM64
nativo, mismo protocolo TDS). En una máquina x64 puede usarse
`mcr.microsoft.com/mssql/server:2022-latest` con los mismos tests. Los
comandos exactos están en el docstring de `tests/integration/test_live_databases.py`.
Verificado además en vivo: `application_name` visible en `pg_stat_activity`
durante el chequeo y cero sesiones remanentes al terminar.

## Fase 3 — Dashboard

### D-023 · Scheduler de disparo único re-armado al finalizar
Cada conexión es un job one-shot de APScheduler que se re-arma al terminar su
chequeo, en lugar de un trigger de intervalo fijo. Consecuencias deseadas: una
conexión jamás se solapa consigo misma; las esperas de cortesía no acumulan
drift (el siguiente slot se calcula desde «ahora»); el backoff es simplemente
otro delay de re-armado; y con `misfire_grace_time=None` los jobs vencidos
durante una suspensión/hibernación se ejecutan al despertar en vez de perderse
(RNF de robustez). El pool de workers es 2× la concurrencia global para que
los hilos esperando cortesía de un host no bloqueen chequeos ejecutables.

### D-024 · «Probar conexión» pasa por el mismo throttle
El botón del formulario ejecuta el chequeo completo a través del mismo
`Throttle.slot(host)` que los chequeos programados: la política de cortesía
aplica también a las pruebas manuales (nunca dos sesiones simultáneas contra
el mismo host, ni siquiera probando). Si se prueba una conexión existente sin
reescribir la contraseña, se reutiliza el secreto guardado (descifrado en el
servidor; nunca viaja al navegador).

### D-025 · `/healthz` sin autenticación
El healthcheck de Docker Compose consulta `/healthz` sin credenciales; el
endpoint solo revela vida del scheduler y versión, nunca datos de conexiones.
Todo lo demás (página y API) exige Basic Auth cuando está habilitada.

### D-026 · Duplicar crea la copia en pausa
La copia apunta al mismo host que el original; crearla activa duplicaría la
carga contra ese host sin que el usuario lo note. Nace `enabled=false` y
conserva el secreto cifrado (mismo almacén, mismo token).

### D-027 · Auto-refresh por polling (10 s), no SSE
Polling simple de `/api/overview`: sobrevive reconexiones, proxies y
suspensión del equipo sin lógica de re-suscripción, y con ≤100 conexiones el
costo por consulta es trivial (3 agregaciones indexadas). La UI usa la fuente
del sistema (`system-ui`) — cero recursos externos sin necesidad de empaquetar
tipografías (el requisito de fuente local queda cubierto).

### D-028 · Secretos nunca salen del servidor
La API expone solo `has_secret`; en edición, `secret=null` significa
«conservar el guardado» y una cadena lo reemplaza. El descifrado ocurre solo
en el proceso del monitor (chequeos y pruebas).

## Fase 4 — Alertas

### D-029 · Anti-spam estructural
El anti-spam de RF-4 no es un filtro: la máquina de incidentes emite
exactamente un `IncidentOpened` y un `IncidentClosed` por caída, así que cada
uno produce una única alerta por canal. Los recordatorios opcionales
(`alerts.reminder_minutes`, 0 = apagado) se controlan con un timestamp por
incidente. Al reiniciar la app con un incidente abierto **no** se re-alerta
(el «último aviso» se inicializa a ahora), pero los recordatorios continúan.

### D-030 · Toda alerta (o su fallo) queda en `alerts_log`
Cada intento por canal registra `ok=0/1` con el detalle del error. Un canal
que falla (SMTP caído, webhook 500, winotify roto) nunca tumba el chequeo ni
bloquea los demás canales.

### D-031 · Sonido con `winsound` (stdlib) y .wav generado localmente
El sonido de alerta usa `winsound` de la stdlib (no agrega dependencia) y un
`static/sounds/alert.wav` de 17 KB generado por script (dos tonos, sin
recursos externos). Suena solo en caída/recordatorio, no en recuperación, y
se puede apagar con `alerts.sound_enabled`.

### D-032 · Contraseña SMTP cifrada con el mismo almacén de secretos
`smtp.password` se guarda como token `dpapi:`/`fernet:` (cifrada al guardar
desde la API) y se descifra solo al enviar. En la API de settings la
contraseña nunca se devuelve; el frontend muestra «(sin cambios)».

### D-033 · Ajustes de cortesía en caliente, concurrencia al reiniciar
`PUT /api/settings` reemplaza la `CourtesyPolicy` del throttle en caliente
(espaciado, rate limit, backoff, jitter). La concurrencia global requiere
reinicio porque el semáforo se dimensiona al arrancar; el formulario lo indica.

### D-034 · Purga nocturna a las 03:30
Job de cron diario que borra checks e incidentes *cerrados* más antiguos que
`retention.days` (default 365). Los incidentes abiertos nunca se purgan.

## Fase 5 — Gráficas y reportes

### D-035 · Chart.js vendorizado; reportes con SVG propio
`static/vendor/chart.umd.min.js` (v4.4.7) se descarga **en build** desde el
tarball de npm y se versiona en el repo: en runtime jamás se toca la red. El
dashboard usa Chart.js; los reportes generan SVG a mano en `reports.py` porque
deben ser un único archivo imprimible sin JavaScript.

### D-036 · Paleta de visualización validada
Colores según el método dataviz (paleta de referencia validada con el script
de seis chequeos: CVD ΔE 73.6, banda de luminosidad y croma OK): latencia =
serie única azul (`#2a78d6` claro / `#3987e5` oscuro, sin leyenda: el título
la nombra); disponibilidad = paleta de **status** reservada
(verde/ámbar/rojo) con leyenda y tooltips como canal secundario. Los reportes
van fijos en modo claro: son documentos para imprimir/enviar, no UI.

### D-037 · Definición de uptime en reportes (RF-6)
Basada en incidentes, impresa en el propio reporte: downtime = suma de la
porción de cada incidente que **solapa** el período; uptime = 1 − downtime ÷
(duración × nº conexiones). Incidentes abiertos cuentan hasta el fin del
período (o «ahora» si es antes). MTTR = media de duración de incidentes
cerrados dentro del período. Días en UTC. El pie del reporte aclara la
granularidad por sondeo y el backoff durante caídas.

### D-038 · Series bucketizadas para las gráficas
`/api/connections/{id}/series` promedia la latencia y agrega la
disponibilidad por bucket (24 h→10 min/1 h; 7 d→1 h/6 h; 30 d→4 h/1 día) en
lugar de enviar hasta ~43 000 checks crudos de 30 días al navegador.

## Fase 6 — Despliegue y cierre

### D-039 · Las conexiones demo nacen pausadas
`--demo` (o `MONITOR_DEMO=1`) siembra 6 conexiones con hosts ficticios y 30
días de historial coherente (checks DOWN dentro de las ventanas de cada
incidente, con la misma causa). Nacen `enabled=false`: si el scheduler las
sondeara, llenaría el historial sintético de incidentes reales contra hosts
inexistentes y arruinaría la demo.

### D-040 · Restore crea conexiones en pausa y nunca duplica
El backup JSON excluye secretos por diseño (D-007: DPAPI y Fernet no viajan).
Al importar, cada conexión se crea **pausada** (sin secreto no puede
autenticar; activarla generaría incidentes falsos) y se omiten las que ya
existen (misma tupla protocolo+host+puerto+nombre), para que re-importar sea
idempotente.

### D-041 · Contenedor con uid 1000 y healthcheck sin curl
`python:3.12-slim` no trae curl: el healthcheck usa `urllib` de la stdlib.
El usuario no root usa uid 1000 (el uid por defecto en la mayoría de distros)
para que los volúmenes `./data` y `./reports` montados desde el host sean
escribibles sin `chown` manual. `MONITOR_DATA_DIR=/app` hace que data/,
reports/ y logs/ cuelguen del punto de montaje.

### D-042 · build.ps1 corre los tests antes de empaquetar
El paquete de Modo A solo se genera si `pytest` pasa en la máquina de build.
Los imports perezosos de la capa Windows (win32crypt, winotify, winsound,
pystray, PIL) se declaran como `--hidden-import` porque PyInstaller no los ve
en el grafo estático; `apscheduler` y `oracledb` van con `--collect-submodules`
(cargan módulos dinámicamente).

### D-043 · Alcance de la verificación final
Todo lo verificable en este entorno se ejecutó en vivo (ver
`docs/ACCEPTANCE.md`): Modo B completo con Docker real, los cinco motores de
BD contra contenedores, cortesía con hilos reales, reporte autocontenido.
Los criterios intrínsecamente de Windows (autoarranque tras reinicio, doble
clic en máquina limpia, toasts) quedan cubiertos por diseño y tests
unitarios con mocks, con un guion de smoke test documentado para el destino.

## Post-cierre — Reducción a Modo A (Windows offline) únicamente

### D-048 · Eliminación de los demás modos de despliegue
El proyecto se acotó a su único objetivo real: un ejecutable portable para una
máquina **Windows sin internet**. Se habían explorado otros dos modos de
despliegue (uno en contenedor, otro en la nube con base de datos gestionada);
ambos se descartaron y se eliminó todo el código y los archivos que solo les
servían a ellos: el adaptador de persistencia alternativo (mismo contrato
público que `Database`, pero contra un motor con servidor), el runner de
chequeos por tick externo, los archivos de imagen/orquestación de contenedor,
y las ramas de código por modo en `main.py`, `detect.py`, `config.py` y
`logging_setup.py`. También se quitó `app/keygen.py` (generaba la clave de
cifrado que solo hacía falta en el modo eliminado; en Windows los secretos
usan DPAPI, que no requiere clave) y `jinja2` de las dependencias (no se usaba).

**Qué se conservó y por qué:**
- Los **drivers de BD** (`pg8000`, PyMySQL, python-tds, oracledb) siguen: son
  los *checkers* que monitorean esas bases, funcionalidad central de Modo A.
  `pg8000` lo usaban dos cosas distintas (el checker y el adaptador de
  persistencia eliminado); solo se fue el adaptador.
- **Fernet** (`secrets_fernet.py`) se mantiene como backend de secretos **solo
  para desarrollo/CI no-Windows** (permite correr y probar la app fuera de
  Windows, donde DPAPI no existe). En el ejecutable Windows nunca se usa.
- La **reconstrucción de estado desde el historial** en `IncidentTracker`
  (motivada originalmente por el modo eliminado, donde cada invocación podía
  ser un proceso nuevo) se queda: resuelve un caso real de Modo A — que la
  app, al arrancar sola tras reiniciar Windows a mitad de una racha de
  fallos, no pierda los fallos previos ni re-alerte. Cubierta por
  `test_rebuilds_unconfirmed_streak_after_restart`.

La autenticación del dashboard pasa a ser **opcional** (el dashboard escucha en
`127.0.0.1`); se activa solo si se definen `MONITOR_DASH_USER`/`MONITOR_DASH_PASS`,
o al exponer a la LAN con `MONITOR_BIND_LAN=1`.

## Post-cierre — Empaquetado 100 % offline (wheelhouse + Python vendorizado)

### D-049 · Wheelhouse vendorizado en el repositorio (`wheelhouse/`)
`build.ps1` instala dependencias exclusivamente desde `wheelhouse/` (`pip
install --no-index --find-links wheelhouse`) y falla explícitamente si esa
carpeta no existe, en vez de intentar salir a PyPI. Se vendorizaron 47 wheels
`win_amd64`/`cp312` (22 MB): las de `requirements.txt` y `requirements-dev.txt`
completas, más PyInstaller y sus propias dependencias de empaquetado
(`altgraph`, `pyinstaller-hooks-contrib`, `pefile`, `pywin32-ctypes`,
`setuptools`, `packaging`).

**Trampa de `pip download` documentada para el futuro**: los marcadores de
entorno (`sys_platform == "win32"`, `platform_system == "Windows"`) se evalúan
contra el intérprete que *ejecuta* pip, no contra `--platform`/`--python-version`
del destino. Descargar `-r requirements.txt` directo omite silenciosamente
`pywin32`/`winotify`/`pystray`/`Pillow` (llevan marcador `sys_platform ==
"win32"`) al correr desde macOS/Linux, y descargar `pystray`/`Pillow`/`PyInstaller`
con resolución de dependencias completa arrastra paquetes de *otra* plataforma
(`pyobjc-framework-Quartz` para darwin) que no existen para Windows y rompen la
descarga. Solución aplicada: los paquetes condicionados por plataforma se
descargan por nombre explícito con `--no-deps`, y sus dependencias reales de
Windows (`Pillow`+`six` para `pystray`; `colorama` para `pytest`/`click`;
`tzdata` para `tzlocal`, que a su vez lo requiere `APScheduler`; `pefile`+
`pywin32-ctypes` para PyInstaller) se añaden a mano tras inspeccionar el
`METADATA` de cada wheel. Verificado con un barrido final sobre los 47 wheels
buscando cualquier `Requires-Dist` condicionado a Windows sin resolver.

### D-050 · Instalador oficial de Python vendorizado (`vendor/`)
Se incluye `python-3.12.10-amd64.exe` (descargado directo de
`python.org/ftp`, cabecera PE verificada, SHA256 registrado) para que ni
siquiera la máquina de build necesite internet para obtener Python. `3.12.13`
—la versión usada en desarrollo— solo publica tarball fuente, sin instalador
Windows; se usó `3.12.10` (misma serie `cp312`, compatibilidad de wheels
intacta: los tags de wheel solo distinguen major.minor + ABI, no el parche).

### D-051 · Límite técnico honesto: sin cross-compilation
PyInstaller no soporta compilación cruzada — el `.exe`/`dist/StabilityMonitor/`
solo puede generarse ejecutando `build.ps1` en una máquina Windows real. Desde
un entorno de desarrollo no-Windows (este) es posible preparar y verificar
*todo* lo que el build necesita (wheelhouse, instalador, el propio script),
pero no producir el artefacto final. Documentado explícitamente en vez de
simular un build inexistente.

### D-052 · Ejecutable de Windows construido y publicado por GitHub Actions
Como PyInstaller no compila de forma cruzada y el desarrollo ocurre en
macOS/Linux, el `.exe` se genera en un runner `windows-latest`
(`.github/workflows/build-windows.yml`): al empujar un tag `vX.Y.Z` el
workflow corre `build.ps1` (instala desde `wheelhouse/`, corre los tests,
empaqueta con PyInstaller), comprime `dist\StabilityMonitor\` en un ZIP y lo
publica como GitHub Release con `gh release create` usando el `GITHUB_TOKEN`
del propio workflow (permiso `contents: write`). El equipo destino descarga
ese ZIP ya construido y lo instala offline; nunca necesita internet ni Python.
Esto convierte el flujo de las Releases en la vía recomendada y deja el build
manual (Opción B, con el wheelhouse y el instalador de Python vendorizados)
como alternativa para quien deba construir en su propia máquina Windows.
`build.ps1` se hizo robusto para ambos entornos: usa el launcher `py -3.12`
si existe y cae a `python` (el runner de CI) si no.

### D-053 · Alias virtuales como metadatos locales
Los alias virtuales se guardan en `connections.aliases_json` y viajan por API,
backup/restore, dashboard, alertas, CSV y reportes. No forman parte de la
conexión técnica: los checkers siguen recibiendo el mismo protocolo, host,
puerto, usuario, objetivos y query. Editar solo alias no reprogama el job del
scheduler, para evitar sesiones adicionales o cambios de carga sobre sistemas
monitoreados. Los nombres se normalizan a Unicode NFC, se validan contra
duplicados y secuencias inseguras, y soportan estado activo/inactivo por alias.

### D-054 · Exportación PDF local desde los reportes
Los reportes siguen generando HTML autocontenido con gráficas SVG, y además se
genera un PDF descargable junto al HTML en `reports/`. El PDF se renderiza
localmente con Pillow, ya presente en las dependencias Windows del ejecutable,
para no depender de navegador, internet, servicios externos ni librerías no
vendorizadas.
