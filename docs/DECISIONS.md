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
