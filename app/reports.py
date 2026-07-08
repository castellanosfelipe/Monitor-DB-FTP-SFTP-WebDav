"""Client stability reports (RF-6): one self-contained HTML file.

Everything is inline — CSS, SVG charts, base64 logo — so the file opens
offline and prints to PDF from any browser.

Uptime definition (also printed inside the report): an incident's downtime
runs from the first confirmed failed check to the first successful check
after it; the client's uptime is ``1 − Σdowntime / (period × connections)``.
Incidents are clipped to the report period; open incidents count until the
end of the period (or "now" if earlier).
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app import __version__, config
from app.db import Database
from app.util import from_iso, to_iso, utc_now

# Chart ink (dataviz reference palette, light mode — reports are print-first)
_SERIES = "#2a78d6"  # latency line (slot 1 blue)
_CONTEXT = "#c3c2b7"  # previous-period context line
_GRID = "#e1e0d9"
_MUTED = "#898781"
_INK = "#0b0b0b"
_INK_2 = "#52514e"
_GOOD = "#0ca30c"
_WARNING = "#fab219"
_CRITICAL = "#d03b3b"
_GOOD_TEXT = "#006300"

_ERROR_LABELS = {
    "dns": "resolución DNS",
    "tcp_connect": "conexión rechazada/inalcanzable",
    "tcp_timeout": "timeout TCP",
    "tls": "error TLS",
    "auth": "autenticación",
    "target_missing": "ruta/objeto inexistente",
    "db_missing": "base de datos inexistente",
    "permission": "permiso denegado",
    "query_timeout": "timeout de query",
    "latency": "latencia sobre el umbral",
    "protocol": "error de protocolo",
    "unknown": "desconocida",
}


@dataclass(frozen=True)
class Branding:
    company: str = ""
    accent: str = "#2563eb"
    logo_b64: str = ""


@dataclass
class IncidentRow:
    connection: str
    aliases: list[str]
    started_at: datetime
    ended_at: datetime | None
    duration_in_period_s: float
    error_type: str | None
    message: str


@dataclass
class ConnStats:
    name: str
    aliases: list[str]
    protocol: str
    interval_s: int
    uptime_pct: float
    downtime_s: float
    incidents: int
    mttr_s: float | None


@dataclass
class PeriodStats:
    client: str
    start: datetime
    end: datetime
    connections: list[ConnStats]
    incidents: list[IncidentRow]
    uptime_pct: float
    downtime_s: float
    incident_count: int
    mttr_s: float | None
    daily_uptime: list[tuple[date, float]]
    daily_latency: list[tuple[date, float | None]]


def _overlap_seconds(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> float:
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    return max(0.0, (end - start).total_seconds())


def compute_period_stats(
    db: Database, client: str, start: datetime, end: datetime, now: datetime | None = None
) -> PeriodStats:
    now = now or utc_now()
    end_effective = min(end, now)
    period_s = max(1.0, (end_effective - start).total_seconds())
    connections = [c for c in db.list_connections() if c.client == client]
    if not connections:
        raise ValueError(f"El cliente '{client}' no tiene conexiones configuradas.")

    conn_stats: list[ConnStats] = []
    all_incidents: list[IncidentRow] = []
    total_downtime = 0.0
    closed_durations: list[float] = []
    incident_count = 0

    for cfg in connections:
        downtime = 0.0
        count = 0
        conn_closed: list[float] = []
        for row in db.list_incidents(cfg.id):
            inc_start = from_iso(row["started_at"])
            inc_end = from_iso(row["ended_at"]) if row["ended_at"] else now
            in_period = _overlap_seconds(inc_start, inc_end, start, end_effective)
            if in_period <= 0:
                continue
            count += 1
            downtime += in_period
            if row["ended_at"] and start <= inc_end <= end_effective:
                conn_closed.append(row["duration_s"] or 0.0)
            all_incidents.append(
                IncidentRow(
                    connection=cfg.name,
                    aliases=cfg.active_aliases,
                    started_at=inc_start,
                    ended_at=from_iso(row["ended_at"]) if row["ended_at"] else None,
                    duration_in_period_s=in_period,
                    error_type=row["error_type"],
                    message=row["first_error_msg"],
                )
            )
        uptime_pct = 100.0 * (1.0 - downtime / period_s)
        conn_stats.append(
            ConnStats(
                name=cfg.name,
                aliases=cfg.active_aliases,
                protocol=cfg.protocol.value,
                interval_s=cfg.interval_s,
                uptime_pct=uptime_pct,
                downtime_s=downtime,
                incidents=count,
                mttr_s=(sum(conn_closed) / len(conn_closed)) if conn_closed else None,
            )
        )
        total_downtime += downtime
        closed_durations.extend(conn_closed)
        incident_count += count

    n = len(connections)
    uptime_pct = 100.0 * (1.0 - total_downtime / (period_s * n))

    # Daily buckets (UTC days), clipped to the effective period.
    daily_uptime: list[tuple[date, float]] = []
    day = start.date()
    while day <= (end_effective - timedelta(microseconds=1)).date():
        day_start = max(start, datetime(day.year, day.month, day.day, tzinfo=timezone.utc))
        day_end = min(end_effective, day_start.replace(hour=0, minute=0, second=0) + timedelta(days=1))
        day_s = max(1.0, (day_end - day_start).total_seconds())
        day_down = 0.0
        for inc in all_incidents:
            inc_end = inc.ended_at or now
            day_down += _overlap_seconds(inc.started_at, inc_end, day_start, day_end)
        daily_uptime.append((day, 100.0 * (1.0 - day_down / (day_s * n))))
        day += timedelta(days=1)

    # Daily average latency across the client's connections.
    ids = ",".join(str(c.id) for c in connections)
    rows = db.execute(
        f"SELECT substr(ts_utc, 1, 10) AS day, AVG(latency_ms) AS avg_ms "
        f"FROM checks WHERE connection_id IN ({ids}) "
        f"AND ts_utc >= ? AND ts_utc < ? AND latency_ms IS NOT NULL GROUP BY day",
        (to_iso(start), to_iso(end_effective)),
    )
    by_day = {r["day"]: r["avg_ms"] for r in rows}
    daily_latency = [(d, by_day.get(d.isoformat())) for d, _ in daily_uptime]

    all_incidents.sort(key=lambda i: i.started_at)
    return PeriodStats(
        client=client,
        start=start,
        end=end_effective,
        connections=conn_stats,
        incidents=all_incidents,
        uptime_pct=uptime_pct,
        downtime_s=total_downtime,
        incident_count=incident_count,
        mttr_s=(sum(closed_durations) / len(closed_durations)) if closed_durations else None,
        daily_uptime=daily_uptime,
        daily_latency=daily_latency,
    )


# --- formatting helpers ------------------------------------------------------------


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.1f} min"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} días"


def fmt_pct(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".") + " %"


def _fmt_dt(dt: datetime | None) -> str:
    return dt.strftime("%d/%m/%Y %H:%M") if dt else "—"


def _esc(text: str) -> str:
    return html.escape(str(text), quote=True)


# --- SVG charts (inline, print-friendly, dataviz specs) ------------------------------


def _rounded_top_bar(x: float, y: float, w: float, h: float, r: float = 4.0) -> str:
    """Bar with 4px rounded data-end and a square baseline."""
    if h <= r:
        return f'M{x},{y + h} v{-h} h{w} v{h} Z'
    return (
        f"M{x},{y + h} v{-(h - r)} q0,-{r} {r},-{r} h{w - 2 * r} "
        f"q{r},0 {r},{r} v{h - r} Z"
    )


def svg_daily_availability(daily: list[tuple[date, float]]) -> str:
    width, height, pad_l, pad_b, pad_t = 720, 180, 44, 26, 10
    plot_w, plot_h = width - pad_l - 12, height - pad_b - pad_t
    n = max(1, len(daily))
    slot = plot_w / n
    bar_w = min(24.0, max(3.0, slot - 2.0))  # ≤24px thick, 2px surface gap

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Disponibilidad diaria" style="width:100%;height:auto">'
    ]
    for frac in (0.0, 0.5, 1.0):
        y = pad_t + plot_h * (1 - frac)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - 12}" y2="{y:.1f}" '
            f'stroke="{_GRID}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="11" fill="{_MUTED}">{frac * 100:.0f}%</text>'
        )
    worst = min(daily, key=lambda d: d[1]) if daily else None
    label_step = max(1, n // 10)
    for i, (day, pct) in enumerate(daily):
        h = plot_h * max(0.0, min(pct, 100.0)) / 100.0
        x = pad_l + i * slot + (slot - bar_w) / 2
        y = pad_t + plot_h - h
        color = _GOOD if pct >= 99.0 else (_WARNING if pct >= 95.0 else _CRITICAL)
        title = f"{day.strftime('%d/%m/%Y')}: {fmt_pct(pct)}"
        parts.append(
            f'<path d="{_rounded_top_bar(x, y, bar_w, h)}" fill="{color}">'
            f"<title>{_esc(title)}</title></path>"
        )
        if i % label_step == 0:
            parts.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{height - 8}" text-anchor="middle" '
                f'font-size="10" fill="{_MUTED}">{day.strftime("%d/%m")}</text>'
            )
        # Selective direct label: only the worst day carries its value.
        if worst is not None and day == worst[0] and pct < 100.0:
            parts.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{max(pad_t + 10, y - 4):.1f}" '
                f'text-anchor="middle" font-size="10" fill="{_INK_2}">{fmt_pct(pct)}</text>'
            )
    parts.append("</svg>")
    return "".join(parts)


def _nice_ceiling(value: float) -> float:
    if value <= 0:
        return 1.0
    magnitude = 10 ** max(0, len(str(int(value))) - 1)
    for mult in (1, 2, 5, 10):
        if value <= mult * magnitude:
            return float(mult * magnitude)
    return float(10 * magnitude)


def _polyline(points: list[tuple[float, float] | None], color: str, width: float) -> str:
    """Polyline segments that break where data is missing."""
    parts = []
    segment: list[str] = []
    for point in points + [None]:
        if point is None:
            if len(segment) >= 2:
                parts.append(
                    f'<polyline points="{" ".join(segment)}" fill="none" '
                    f'stroke="{color}" stroke-width="{width}" '
                    f'stroke-linejoin="round" stroke-linecap="round"/>'
                )
            elif len(segment) == 1:
                x, y = segment[0].split(",")
                parts.append(f'<circle cx="{x}" cy="{y}" r="3" fill="{color}"/>')
            segment = []
        else:
            segment.append(f"{point[0]:.1f},{point[1]:.1f}")
    return "".join(parts)


def svg_daily_latency(
    current: list[tuple[date, float | None]],
    previous: list[tuple[date, float | None]] | None,
) -> str:
    width, height, pad_l, pad_b, pad_t = 720, 200, 52, 40, 10
    plot_w, plot_h = width - pad_l - 16, height - pad_b - pad_t
    n = max(1, len(current))
    values = [v for _, v in current if v is not None]
    prev_values = [v for _, v in (previous or []) if v is not None]
    y_max = _nice_ceiling(max(values + prev_values, default=1.0) * 1.15)

    def xy(index: int, value: float) -> tuple[float, float]:
        x = pad_l + (plot_w * index / max(1, n - 1) if n > 1 else plot_w / 2)
        return x, pad_t + plot_h * (1 - value / y_max)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Latencia promedio diaria (ms)" style="width:100%;height:auto">'
    ]
    for frac in (0.0, 0.5, 1.0):
        y = pad_t + plot_h * (1 - frac)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - 16}" y2="{y:.1f}" '
            f'stroke="{_GRID}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end" font-size="11" '
            f'fill="{_MUTED}">{y_max * frac:,.0f}</text>'
        )
    label_step = max(1, n // 10)
    for i, (day, _) in enumerate(current):
        if i % label_step == 0:
            x = xy(i, 0)[0]
            parts.append(
                f'<text x="{x:.1f}" y="{pad_t + plot_h + 16}" text-anchor="middle" '
                f'font-size="10" fill="{_MUTED}">{day.strftime("%d/%m")}</text>'
            )
    if previous:
        prev_points = [
            xy(i, v) if v is not None else None for i, (_, v) in enumerate(previous[:n])
        ]
        parts.append(_polyline(prev_points, _CONTEXT, 2))
    points = [xy(i, v) if v is not None else None for i, (_, v) in enumerate(current)]
    parts.append(_polyline(points, _SERIES, 2))
    last = next(((i, v) for i, (_, v) in reversed(list(enumerate(current))) if v is not None), None)
    if last is not None:
        x, y = xy(last[0], last[1])
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{_SERIES}" stroke="#ffffff" stroke-width="2"/>')
        parts.append(
            f'<text x="{min(x + 8, width - 60):.1f}" y="{y - 8:.1f}" font-size="11" '
            f'fill="{_INK_2}">{last[1]:,.0f} ms</text>'
        )
    # Legend (two series when previous period is shown)
    legend_y = height - 6
    parts.append(
        f'<rect x="{pad_l}" y="{legend_y - 9}" width="14" height="3" fill="{_SERIES}"/>'
        f'<text x="{pad_l + 20}" y="{legend_y}" font-size="11" fill="{_INK_2}">Período actual</text>'
    )
    if previous:
        parts.append(
            f'<rect x="{pad_l + 130}" y="{legend_y - 9}" width="14" height="3" fill="{_CONTEXT}"/>'
            f'<text x="{pad_l + 150}" y="{legend_y}" font-size="11" fill="{_INK_2}">Período anterior</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


# --- report document -------------------------------------------------------------------


def _delta(value: float | None, previous: float | None, unit: str, up_is_good: bool) -> str:
    if value is None or previous is None:
        return '<span class="delta muted">sin período anterior</span>'
    diff = value - previous
    if abs(diff) < 1e-9:
        return '<span class="delta muted">sin cambios vs período anterior</span>'
    good = (diff > 0) == up_is_good
    color = _GOOD_TEXT if good else _CRITICAL
    arrow = "▲" if diff > 0 else "▼"
    if unit == "pp":
        text = f"{abs(diff):.2f} pp"
    elif unit == "s":
        text = fmt_duration(abs(diff))
    else:
        text = f"{abs(diff):.0f}"
    return f'<span class="delta" style="color:{color}">{arrow} {text} vs período anterior</span>'


def render_report(stats: PeriodStats, previous: PeriodStats | None, branding: Branding) -> str:
    accent = branding.accent or "#2563eb"
    company = branding.company or "StabilityMonitor"
    logo = (
        f'<img src="data:image/png;base64,{branding.logo_b64}" alt="" style="height:42px">'
        if branding.logo_b64
        else ""
    )
    min_interval = min((c.interval_s for c in stats.connections), default=60)

    incident_rows = "".join(
        f"<tr><td>{_esc(i.connection)}</td><td>{_esc(', '.join(i.aliases) or '—')}</td>"
        f"<td>{_fmt_dt(i.started_at)}</td>"
        f"<td>{_fmt_dt(i.ended_at) if i.ended_at else '<b>abierto</b>'}</td>"
        f"<td>{fmt_duration(i.duration_in_period_s)}</td>"
        f"<td>{_esc(_ERROR_LABELS.get(i.error_type or '', i.error_type or '—'))}</td>"
        f"<td class='msg'>{_esc(i.message)}</td></tr>"
        for i in stats.incidents
    ) or '<tr><td colspan="7" class="muted">Sin incidentes en el período 🎉</td></tr>'

    conn_rows = "".join(
        f"<tr><td>{_esc(c.name)}</td><td>{_esc(', '.join(c.aliases) or '—')}</td>"
        f"<td>{_esc(c.protocol)}</td>"
        f"<td>{fmt_pct(c.uptime_pct)}</td><td>{c.incidents}</td>"
        f"<td>{fmt_duration(c.downtime_s) if c.downtime_s else '—'}</td>"
        f"<td>{fmt_duration(c.mttr_s)}</td></tr>"
        for c in stats.connections
    )

    prev_uptime = previous.uptime_pct if previous else None
    prev_downtime = previous.downtime_s if previous else None
    prev_incidents = float(previous.incident_count) if previous else None
    prev_mttr = previous.mttr_s if previous else None

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reporte de estabilidad — {_esc(stats.client)}</title>
<style>
  body {{ margin:0; padding:32px; color:{_INK}; background:#ffffff;
         font:14px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
  .head {{ display:flex; align-items:center; gap:16px; border-bottom:3px solid {accent};
           padding-bottom:14px; margin-bottom:22px; }}
  .head h1 {{ font-size:20px; margin:0; }}
  .head .sub {{ color:{_INK_2}; font-size:13px; }}
  h2 {{ font-size:15px; margin:26px 0 10px; }}
  .kpis {{ display:flex; gap:14px; flex-wrap:wrap; }}
  .kpi {{ flex:1; min-width:150px; border:1px solid {_GRID}; border-radius:10px; padding:12px 14px; }}
  .kpi .label {{ font-size:12px; color:{_INK_2}; }}
  .kpi .value {{ font-size:30px; font-weight:600; margin:2px 0; }}
  .kpi .delta {{ font-size:11.5px; }}
  .muted {{ color:{_MUTED}; }}
  .table-scroll {{ max-width:100%; max-height:420px; overflow:auto; border:1px solid {_GRID};
                   border-radius:8px; margin-top:6px; }}
  table {{ width:100%; border-collapse:collapse; font-size:12.5px; margin-top:6px; }}
  .table-scroll table {{ min-width:760px; margin:0; }}
  th, td {{ text-align:left; padding:6px 8px; border-bottom:1px solid {_GRID};
            vertical-align:top; }}
  th {{ color:{_INK_2}; font-weight:600; background:#ffffff; }}
  .table-scroll tr:first-child th {{ position:sticky; top:0; z-index:1; }}
  td.msg {{ color:{_INK_2}; max-width:340px; word-break:break-word; }}
  .foot {{ margin-top:30px; padding-top:12px; border-top:1px solid {_GRID};
           font-size:11.5px; color:{_INK_2}; }}
  @media print {{ body {{ padding:12mm; }} .kpi {{ break-inside:avoid; }} }}
</style>
</head>
<body>
  <div class="head">
    {logo}
    <div>
      <h1>Reporte de estabilidad — {_esc(stats.client)}</h1>
      <div class="sub">{_esc(company)} · Período: {stats.start.strftime('%d/%m/%Y')} –
        {(stats.end - timedelta(microseconds=1)).strftime('%d/%m/%Y')} (UTC) ·
        Generado el {utc_now().strftime('%d/%m/%Y %H:%M')} UTC</div>
    </div>
  </div>

  <h2>Resumen ejecutivo</h2>
  <div class="kpis">
    <div class="kpi"><div class="label">Disponibilidad</div>
      <div class="value">{fmt_pct(stats.uptime_pct)}</div>
      {_delta(stats.uptime_pct, prev_uptime, "pp", up_is_good=True)}</div>
    <div class="kpi"><div class="label">Incidentes</div>
      <div class="value">{stats.incident_count}</div>
      {_delta(float(stats.incident_count), prev_incidents, "n", up_is_good=False)}</div>
    <div class="kpi"><div class="label">Downtime total</div>
      <div class="value" style="font-size:22px">{fmt_duration(stats.downtime_s) if stats.downtime_s else "0 s"}</div>
      {_delta(stats.downtime_s, prev_downtime, "s", up_is_good=False)}</div>
    <div class="kpi"><div class="label">MTTR (tiempo medio de recuperación)</div>
      <div class="value" style="font-size:22px">{fmt_duration(stats.mttr_s)}</div>
      {_delta(stats.mttr_s, prev_mttr, "s", up_is_good=False)}</div>
  </div>

  <h2>Disponibilidad diaria</h2>
  {svg_daily_availability(stats.daily_uptime)}

  <h2>Latencia promedio diaria (ms)</h2>
  {svg_daily_latency(stats.daily_latency, previous.daily_latency if previous else None)}

  <h2>Detalle por conexión</h2>
  <div class="table-scroll"><table>
    <tr><th>Conexión</th><th>Alias</th><th>Protocolo</th><th>Disponibilidad</th>
        <th>Incidentes</th><th>Downtime</th><th>MTTR</th></tr>
    {conn_rows}
  </table></div>

  <h2>Incidentes del período</h2>
  <div class="table-scroll"><table>
    <tr><th>Conexión</th><th>Alias</th><th>Inicio</th><th>Fin</th><th>Duración en el período</th>
        <th>Causa</th><th>Detalle</th></tr>
    {incident_rows}
  </table></div>

  <div class="foot">
    <b>Metodología.</b> El downtime de un incidente se mide desde el primer chequeo
    fallido confirmado hasta el primer chequeo exitoso posterior; la disponibilidad
    del período es 1 − (downtime acumulado ÷ (duración del período × número de
    conexiones)). La medición se basa en sondeos periódicos (intervalo mínimo
    configurado: {min_interval} s); durante una caída los sondeos se espacian
    progresivamente (backoff) hasta un tope configurable, por lo que el instante de
    recuperación —y por tanto el MTTR— tiene esa granularidad. Todas las horas están
    en UTC. Reporte generado por StabilityMonitor v{__version__}; este archivo es
    autocontenido y no requiere conexión a internet.
  </div>
</body>
</html>"""


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "cliente"


def _pdf_font(size: int, bold: bool = False):
    from PIL import ImageFont

    candidates = [
        Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf"),
        Path("C:/Windows/Fonts/tahomabd.ttf" if bold else "C:/Windows/Fonts/tahoma.ttf"),
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simsun.ttc"),
    ]
    for path in candidates:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def _text_width(draw, text: str, font) -> float:
    box = draw.textbbox((0, 0), text, font=font)
    return float(box[2] - box[0])


def _wrap_pdf_text(draw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for raw_line in str(text).splitlines() or [""]:
        words = raw_line.split(" ")
        current = ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            if _text_width(draw, candidate, font) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
            current = word
            while _text_width(draw, current, font) > max_width and len(current) > 1:
                cut = len(current)
                while cut > 1 and _text_width(draw, current[:cut], font) > max_width:
                    cut -= 1
                lines.append(current[:cut])
                current = current[cut:]
        lines.append(current)
    return lines


def render_report_pdf(
    stats: PeriodStats,
    previous: PeriodStats | None,
    branding: Branding,
    path: Path,
) -> Path:
    """Render a real offline PDF alongside the HTML report."""
    from PIL import Image, ImageColor, ImageDraw

    page_w, page_h = 1240, 1754  # A4 at 150 dpi
    margin = 82
    content_w = page_w - 2 * margin
    ink = (16, 24, 32)
    muted = (90, 96, 106)
    grid = (220, 224, 229)
    try:
        accent = ImageColor.getrgb(branding.accent or "#2563eb")
    except Exception:
        accent = (37, 99, 235)

    title_font = _pdf_font(34, bold=True)
    h_font = _pdf_font(22, bold=True)
    body_font = _pdf_font(17)
    small_font = _pdf_font(14)
    bold_font = _pdf_font(17, bold=True)

    pages: list[Image.Image] = []
    image = Image.new("RGB", (page_w, page_h), "white")
    draw = ImageDraw.Draw(image)
    y = margin

    def new_page() -> None:
        nonlocal image, draw, y
        pages.append(image)
        image = Image.new("RGB", (page_w, page_h), "white")
        draw = ImageDraw.Draw(image)
        y = margin

    def ensure(height: int) -> None:
        if y + height > page_h - margin:
            new_page()

    def line(text: str, font=body_font, fill=ink, gap: int = 8, indent: int = 0) -> None:
        nonlocal y
        wrapped = _wrap_pdf_text(draw, text, font, content_w - indent)
        line_h = max(22, int(font.size * 1.35) if hasattr(font, "size") else 22)
        ensure(max(1, len(wrapped)) * line_h + gap)
        for part in wrapped:
            draw.text((margin + indent, y), part, font=font, fill=fill)
            y += line_h
        y += gap

    def heading(text: str) -> None:
        nonlocal y
        ensure(54)
        y += 12
        draw.text((margin, y), text, font=h_font, fill=ink)
        y += 38
        draw.line((margin, y, page_w - margin, y), fill=grid, width=2)
        y += 14

    company = branding.company or "StabilityMonitor"
    line(f"Reporte de estabilidad - {stats.client}", title_font, ink, gap=2)
    draw.rectangle((margin, y, page_w - margin, y + 6), fill=accent)
    y += 18
    line(
        f"{company} | Periodo: {stats.start:%d/%m/%Y} - "
        f"{(stats.end - timedelta(microseconds=1)):%d/%m/%Y} UTC | "
        f"Generado: {utc_now():%d/%m/%Y %H:%M} UTC",
        small_font,
        muted,
        gap=16,
    )

    heading("Resumen ejecutivo")
    kpis = [
        ("Disponibilidad", fmt_pct(stats.uptime_pct)),
        ("Incidentes", str(stats.incident_count)),
        ("Downtime total", fmt_duration(stats.downtime_s) if stats.downtime_s else "0 s"),
        ("MTTR", fmt_duration(stats.mttr_s)),
    ]
    for label, value in kpis:
        line(f"{label}: {value}", bold_font, ink, gap=2)
    if previous is not None:
        line(
            f"Comparacion: periodo anterior con disponibilidad {fmt_pct(previous.uptime_pct)}, "
            f"{previous.incident_count} incidentes y downtime {fmt_duration(previous.downtime_s)}.",
            small_font,
            muted,
            gap=10,
        )

    heading("Detalle por conexion")
    for conn in stats.connections:
        alias = ", ".join(conn.aliases) or "sin alias"
        line(
            f"{conn.name} | Alias: {alias} | {conn.protocol} | "
            f"Disponibilidad {fmt_pct(conn.uptime_pct)} | Incidentes {conn.incidents} | "
            f"Downtime {fmt_duration(conn.downtime_s) if conn.downtime_s else '0 s'} | "
            f"MTTR {fmt_duration(conn.mttr_s)}",
            body_font,
            ink,
            gap=6,
        )

    heading("Incidentes del periodo")
    if not stats.incidents:
        line("Sin incidentes en el periodo.", body_font, muted)
    for inc in stats.incidents:
        alias = ", ".join(inc.aliases) or "sin alias"
        line(
            f"{inc.connection} | Alias: {alias} | Inicio {_fmt_dt(inc.started_at)} | "
            f"Fin {_fmt_dt(inc.ended_at) if inc.ended_at else 'abierto'} | "
            f"Duracion {fmt_duration(inc.duration_in_period_s)} | "
            f"Causa {_ERROR_LABELS.get(inc.error_type or '', inc.error_type or 'desconocida')}",
            body_font,
            ink,
            gap=2,
        )
        if inc.message:
            line(f"Detalle: {inc.message}", small_font, muted, gap=8, indent=24)

    heading("Metodologia")
    min_interval = min((c.interval_s for c in stats.connections), default=60)
    line(
        "El downtime se mide desde el primer chequeo fallido confirmado hasta "
        "el primer chequeo exitoso posterior. Los alias son metadatos locales: "
        "no cambian host, puerto, usuario, rutas, base de datos ni consultas de salud.",
        small_font,
        muted,
        gap=8,
    )
    line(
        f"Intervalo minimo configurado: {min_interval} s. Reporte generado por "
        f"StabilityMonitor v{__version__}; no requiere conexion a internet.",
        small_font,
        muted,
        gap=8,
    )

    pages.append(image)
    for index, page in enumerate(pages, start=1):
        d = ImageDraw.Draw(page)
        d.text(
            (page_w - margin - 110, page_h - margin + 30),
            f"Pagina {index}/{len(pages)}",
            font=small_font,
            fill=muted,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(path, "PDF", save_all=True, append_images=pages[1:], resolution=150.0)
    return path


def generate_report(
    db: Database,
    client: str,
    start_date: date,
    end_date: date,
    branding: Branding,
    now: datetime | None = None,
) -> Path:
    """Generate the report file for [start_date, end_date] (inclusive, UTC)."""
    if end_date < start_date:
        raise ValueError("La fecha final debe ser posterior a la inicial.")
    start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc)
    end = datetime(end_date.year, end_date.month, end_date.day, tzinfo=timezone.utc) + timedelta(days=1)
    stats = compute_period_stats(db, client, start, end, now=now)

    length = end - start
    try:
        previous = compute_period_stats(db, client, start - length, start, now=now)
    except ValueError:
        previous = None

    html_doc = render_report(stats, previous, branding)
    filename = f"reporte_{_slug(client)}_{start_date:%Y%m%d}_{end_date:%Y%m%d}.html"
    path = config.reports_dir() / filename
    path.write_text(html_doc, encoding="utf-8")
    render_report_pdf(stats, previous, branding, path.with_suffix(".pdf"))
    return path
