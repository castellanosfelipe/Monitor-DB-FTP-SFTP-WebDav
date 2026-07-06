# Despliegue serverless — Vercel + Neon

Tercer modo de despliegue (además del Modo A Windows y el Modo B Docker):
el dashboard y la API corren como funciones serverless en Vercel y la
persistencia vive en Neon (PostgreSQL). **No hay proceso residente**: los
chequeos se ejecutan cuando algo llama a `/api/cron/tick`.

## Estado actual (ya desplegado)

| Recurso | Valor |
|---|---|
| URL de producción | https://stability-monitor-product15.vercel.app |
| Proyecto Vercel | `product15/stability-monitor` |
| Proyecto Neon | `stability-monitor` (Postgres 17, us-east-2) |
| Usuario del dashboard | `admin` (contraseña en las variables de entorno de Vercel) |

Variables configuradas en Vercel (Production): `DATABASE_URL` (pooler de
Neon), `MONITOR_SECRET_KEY` (Fernet), `MONITOR_DASH_USER`,
`MONITOR_DASH_PASS`, `CRON_SECRET`.

## Cómo se disparan los chequeos

`GET/POST /api/cron/tick` con cabecera `Authorization: Bearer <CRON_SECRET>`
ejecuta todos los chequeos **vencidos** (los más atrasados primero) dentro de
un presupuesto de ~45 s; lo que no alcance se difiere al siguiente tick. El
backoff durante caídas y la histéresis funcionan igual que en los otros
modos: la máquina de incidentes reconstruye su estado desde la base de datos
en cada invocación.

Planificadores, de mejor a suficiente:

1. **Vercel Cron (plan Pro)** — cambia el schedule de `vercel.json` a
   `* * * * *` y listo (Vercel envía el `CRON_SECRET` automáticamente).
2. **GitHub Actions (incluido)** — `.github/workflows/monitor-tick.yml` llama
   al tick cada 5 minutos. Requiere crear el secreto del repositorio una vez:

   ```bash
   gh secret set CRON_SECRET --repo castellanosfelipe/Monitor-DB-FTP-SFTP-WebDav
   # pega el mismo valor que la variable CRON_SECRET de Vercel
   # (Vercel → proyecto stability-monitor → Settings → Environment Variables)
   ```

   (o en GitHub: Settings → Secrets and variables → Actions → New repository secret)
3. **Vercel Cron (plan Hobby)** — queda configurado un tick diario (06:00 UTC)
   como respaldo/housekeeping; por sí solo NO da monitoreo continuo.

## Límites de este modo (léelos antes de usarlo en serio)

- **Solo servidores alcanzables desde internet.** Vercel no ve tu LAN; los
  Modos A/B siguen siendo los indicados para redes internas.
- **Granularidad**: la cadencia real la fija el planificador externo
  (≥1 min en Pro, ~5 min con GitHub Actions, que además es «mejor esfuerzo»).
  Los intervalos configurados por conexión actúan como mínimo, no como exacto.
- **Cortesía entre invocaciones**: dentro de un tick aplica la serialización
  por host, el espaciado y el rate limit de siempre; entre ticks el espaciado
  lo impone la propia cadencia del planificador.
- **SFTP/TOFU**: `known_hosts` vive en `/tmp` (efímero); la protección contra
  cambio de clave de host entre cold starts no persiste en este modo.
- **Sin toasts/bandeja**: las alertas son banner del dashboard, `alerts_log`
  y SMTP/webhook si los configuras (deben ser alcanzables desde Vercel).
- La UI de Vercel («Deployment Protection») se deshabilitó para este proyecto:
  la autenticación la hace la propia app (HTTP Basic obligatoria).

## Recrear el despliegue desde cero

```bash
# Neon
npx neonctl@2 projects create --name stability-monitor --org-id <tu-org>
#   → copia el connection string (usa el host *-pooler)

# Vercel
vercel link --project stability-monitor
python -m app.keygen | vercel env add MONITOR_SECRET_KEY production
vercel env add DATABASE_URL production        # postgres://…-pooler…/neondb?sslmode=require
vercel env add MONITOR_DASH_USER production
vercel env add MONITOR_DASH_PASS production
python -c "import secrets;print(secrets.token_urlsafe(24))" | vercel env add CRON_SECRET production
vercel deploy --prod
```

El esquema de la base se crea solo en el primer arranque. Recuerda: sin
`MONITOR_SECRET_KEY` los secretos guardados son irrecuperables.
