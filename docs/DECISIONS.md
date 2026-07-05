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

### D-016 · Nota pendiente sobre `oracledb`
La especificación lo describe como "100 % Python puro"; en realidad
`python-oracledb` incluye una extensión compilada (Cython) aunque el modo thin
no requiera Instant Client. Hay wheels oficiales para Windows x64, así que
PyInstaller lo empaqueta sin fricción. Se validará en la Fase 2/6; si diera
problemas, el plan B es cargarlo como dependencia opcional.
