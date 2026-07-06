# StabilityMonitor — Manual de usuario

Monitor de disponibilidad **de bajo impacto** para servidores de archivos
(FTP, FTPS, SFTP, WebDAV, WebDAVS) y bases de datos (PostgreSQL, MySQL,
MariaDB, SQL Server, Oracle), con historial, incidentes, alertas y reportes
de estabilidad listos para enviar a tus clientes.

> **Filosofía**: el monitor jamás debe ser el problema. Nunca abre dos
> sesiones a la vez contra el mismo servidor, espacia sus chequeos, hace la
> operación mínima posible (nada de listados recursivos ni descargas), se
> identifica en el tráfico como `StabilityMonitor/x.y.z` y, cuando un
> servidor cae, **reduce** la frecuencia de sondeo en lugar de insistir.

---

## Índice

1. [Instalación — Windows 10 Pro sin internet](#modo-a)
2. [Uso del dashboard](#dashboard)
3. [Conexiones: campos y ejemplos](#conexiones)
4. [Alertas](#alertas)
5. [Reportes de estabilidad](#reportes)
6. [Ajustes y respaldo de configuración](#ajustes)
7. [Modo demo](#demo)
8. [Solución de problemas](#problemas)

---

<a name="modo-a"></a>
## 1. Instalación — Windows 10 Pro x64, 100 % offline

La máquina destino **no necesita internet ni Python**, y no hacen falta
permisos de administrador. El paquete se construye una vez en una máquina de
desarrollo con internet y se traslada por USB o red interna.

### 1.1 Construir el paquete (máquina CON internet)

1. Instala [Python 3.12 para Windows x64](https://www.python.org/downloads/)
   (marca «Add python.exe to PATH»).
2. Copia el código fuente del proyecto y abre PowerShell en esa carpeta.
3. Ejecuta:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\build.ps1
   ```

   El script crea un entorno virtual, instala las dependencias fijadas,
   corre los tests y genera **`dist\StabilityMonitor\`** con PyInstaller.

### 1.2 Instalar en la máquina destino (SIN internet)

1. Copia la carpeta completa `dist\StabilityMonitor\` por USB a la máquina
   destino, por ejemplo a `C:\Users\tu-usuario\StabilityMonitor\`.
   Debe quedar en una carpeta donde tu usuario pueda escribir (ahí vivirán
   `data\`, `logs\` y `reports\`).
2. Dentro de esa carpeta, ejecuta:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```

   Esto registra una **tarea programada a nivel de usuario** (sin admin) que
   arranca el monitor al iniciar sesión, y lo inicia inmediatamente.
3. Abre el dashboard: **http://127.0.0.1:8090** (o doble clic en el ícono de
   la bandeja del sistema).

Notas:

- El dashboard solo escucha en `127.0.0.1`. Para exponerlo a la LAN, define
  las variables de entorno `MONITOR_BIND_LAN=1` **y** `MONITOR_DASH_USER` /
  `MONITOR_DASH_PASS` (autenticación) antes de arrancar.
- Los secretos se cifran con **DPAPI**, ligados a esta máquina y usuario de
  Windows: copiar `data\monitor.db` a otra máquina **no** expone las
  contraseñas (ni permite usarlas).
- El **ícono de bandeja** cambia a rojo cuando hay un incidente abierto y
  ofrece: Abrir dashboard · Pausar todo · Reanudar · Salir.
- La app sobrevive reinicios (tarea programada) y suspensiones (los chequeos
  pendientes se ejecutan al despertar y se reprograman con normalidad).
- Para desinstalar: `uninstall.ps1` (quita el autoarranque; los datos quedan).

<a name="dashboard"></a>
## 2. Uso del dashboard

La vista principal muestra **una tarjeta por conexión** con: estado (verde
UP / ámbar DEGRADED / rojo DOWN / gris pausada), disponibilidad 24 h / 7 d /
30 d, latencia del último chequeo y promedio de 24 h, hora del último chequeo
y cliente. Se actualiza sola cada 10 s.

- **Filtros**: por cliente, protocolo y estado; búsqueda por nombre.
- **Banner rojo superior**: incidentes abiertos en este momento.
- **⏸ Pausar todo / ▶ Reanudar**: detiene temporalmente todos los chequeos
  (los que están en curso terminan limpio).
- **Detalle** (en cada tarjeta): gráfica de latencia y línea de tiempo de
  disponibilidad (24 h / 7 d / 30 d), lista de incidentes con causa y
  duración, últimos chequeos, y descarga de **CSV** de checks e incidentes.
- Acciones por tarjeta: **Editar · Duplicar · Pausar/Reanudar · Eliminar**.
  La copia de una conexión nace pausada (apunta al mismo servidor; actívala
  cuando la hayas ajustado).

### Estados

| Estado | Significado |
|---|---|
| **UP** | Conecta, autentica y todos los objetivos verifican. |
| **DEGRADED** | Autentica, pero algún objetivo falla (ruta/tabla inexistente…) o la latencia supera el umbral configurado. |
| **DOWN** | No conecta o no autentica, tras agotar los reintentos. Abre incidente y alerta. |

Un solo chequeo fallido **no** abre incidente: hacen falta `reintentos + 1`
fallos consecutivos (histéresis anti-parpadeo). El incidente se cierra con el
primer chequeo exitoso y la alerta de recuperación indica cuánto duró.

<a name="conexiones"></a>
## 3. Conexiones: campos y ejemplos

**Comunes**: nombre, cliente (agrupa en reportes), protocolo, host, puerto
(se rellena solo según el protocolo), usuario, secreto, intervalo (30 s–1 h),
timeout, reintentos antes de DOWN, umbral DEGRADED en ms (opcional), notas.

**Objetivos a verificar** (opcional, uno por línea):

- Protocolos de archivos → rutas absolutas: `/clientes/acme/entrada`
- Bases de datos → `esquema` o `esquema.tabla`: `ventas`, `ventas.pedidos`

Una ruta o tabla inexistente marca la conexión como **DEGRADED** con causa
`ruta/objeto inexistente` — distinta de «servidor caído», para que el reporte
cuente la historia correcta.

**SFTP**: autenticación por contraseña o por **llave privada** (ruta de la
llave + passphrase como secreto). La primera conexión registra la huella del
servidor; si la clave del host cambia después, el chequeo falla con causa
`tls` (posible reinstalación o suplantación).

**Bases de datos**: nombre de la base (obligatorio en PostgreSQL y Oracle —
en Oracle es el *service name*), `ssl_mode` y **query de salud** opcional con
restricciones duras: debe empezar por `SELECT`, una sola sentencia, se lee
máximo 1 fila y corre con timeout ≤ 5 s. Cualquier otra cosa se rechaza al
guardar **y** al ejecutar.

**Chequeo de escritura** (solo archivos, apagado por defecto): sube un
archivo `.monitor_probe` de menos de 1 KB al primer objetivo y lo borra
siempre, incluso si algo falla.

**Probar conexión**: ejecuta el chequeo completo desde el formulario y
muestra el resultado por objetivo antes de guardar. La prueba respeta la
misma política de cortesía que los chequeos programados.

### Usuario de monitoreo recomendado

Crea en cada servidor un usuario **de solo lectura** dedicado (p. ej.
`monitor`). En bases de datos basta `CONNECT`/`SELECT` sobre los catálogos y
las tablas objetivo. El tráfico del monitor se identifica como
`StabilityMonitor/x.y.z` (User-Agent en WebDAV, `application_name` en
PostgreSQL, `program_name` en MySQL, `appname` en SQL Server, `program` en
Oracle) para que los administradores puedan reconocerlo y filtrarlo.

<a name="alertas"></a>
## 4. Alertas

Cuando se confirma una caída (y cuando se recupera):

- **En Windows**: notificación toast nativa + sonido opcional + ícono de
  bandeja en rojo + banner persistente en el dashboard.
- **SMTP** y **webhook HTTP** (opcionales, apagados por defecto): se
  configuran en ⚙ Ajustes. Como la máquina no tiene internet, solo deben
  apuntar a servidores de la LAN (por ejemplo, un SMTP interno).

Anti-spam: **una sola alerta por incidente**. Si quieres recordatorios
mientras siga caído, actívalos en Ajustes («Recordatorio si sigue caída»).
Toda alerta enviada (o fallida) queda en el registro `alerts_log`.

<a name="reportes"></a>
## 5. Reportes de estabilidad

Botón **📄 Reportes**: elige cliente y período (últimos 7/30 días, mes
anterior o personalizado) y pulsa «Generar reporte».

El resultado es **un único archivo HTML autocontenido** en `reports/` que se
abre sin internet y se imprime a PDF desde el navegador. Incluye: resumen
ejecutivo (disponibilidad, incidentes, downtime total, MTTR) con comparativa
contra el período anterior, gráfica de disponibilidad diaria, gráfica de
latencia, tabla de incidentes con causas y la **metodología del cálculo** al
pie (cómo se mide el downtime, la granularidad del sondeo y el efecto del
backoff).

El branding (nombre de la empresa, color de acento, logo) se configura en
⚙ Ajustes.

<a name="ajustes"></a>
## 6. Ajustes y respaldo de configuración

En ⚙ Ajustes puedes editar:

- **Política de cortesía**: concurrencia global (requiere reinicio),
  espaciado por host, máximo de chequeos por host/minuto, tope de backoff.
  Bajar estos valores hace al monitor aún más conservador.
- **Retención** del historial (default 365 días; purga nocturna automática).
- **Alertas**: recordatorios, SMTP y webhook.
- **Branding** de reportes.

**Respaldo**: «Exportar JSON» descarga conexiones y ajustes. Los **secretos
no se exportan** (el cifrado DPAPI está ligado a la máquina y usuario de
Windows, no es transferible): al importar en otra máquina, las conexiones se
crean **pausadas** y deberás reingresar las contraseñas y reanudarlas.

<a name="demo"></a>
## 7. Modo demo

Para evaluar el dashboard, las gráficas y los reportes sin servidores reales:

```bash
python -m app.main --demo
```

Siembra 6 conexiones ficticias de dos clientes (archivos y bases de datos)
con 30 días de historial sintético, incidentes variados y uno abierto. Las
conexiones demo nacen **pausadas** (sus hosts no existen); puedes borrarlas
desde el dashboard cuando termines.

<a name="problemas"></a>
## 8. Solución de problemas

| Síntoma | Causa probable / solución |
|---|---|
| El dashboard no abre en `127.0.0.1:8090` | ¿Otro proceso usa el puerto? Define `MONITOR_PORT` con otro valor. Revisa `logs\app.log`. |
| «No se pudo descifrar el secreto…» | La carpeta `data\` se copió desde otra máquina o usuario de Windows (DPAPI está ligado a ambos). Reingresa la credencial en la conexión. |
| Una conexión SFTP falla con causa `tls` | La clave del host cambió (¿reinstalación del servidor?). Si es legítimo, borra la línea de ese host en `data\known_hosts`. |
| Muchas conexiones al mismo host van «lentas» | Es la política de cortesía: se serializan y espacian a propósito. Puedes relajar el espaciado en Ajustes, con criterio. |
| La detección de una caída tarda | Por diseño tarda hasta `intervalo × (reintentos + 1) + timeout`. Baja intervalo o reintentos si necesitas detectar antes. |
| El reporte no muestra un cliente | El campo «cliente» de las conexiones debe coincidir exactamente. |
| Windows recién reiniciado y no arranca | Verifica la tarea: `Get-ScheduledTask StabilityMonitor` en PowerShell; re-ejecuta `install.ps1` si no existe. |

### Archivos que importan

| Ruta | Contenido |
|---|---|
| `data/monitor.db` | Historial, conexiones y secretos **cifrados** (SQLite WAL). |
| `data/known_hosts` | Huellas SSH aprendidas (SFTP). |
| `logs/app.log` | Log técnico rotativo. |
| `reports/*.html` | Reportes generados (autocontenidos). |

El respaldo correcto es: la carpeta `data\` completa **más** esta guía de
reinstalación. Recuerda que los secretos DPAPI solo se descifran en la misma
máquina y usuario de Windows; al restaurar en otra máquina deberás
reingresarlos.
