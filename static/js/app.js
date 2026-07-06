/* StabilityMonitor dashboard — vanilla JS, sin dependencias externas. */
"use strict";

const FILE_PROTOCOLS = new Set(["FTP", "FTPS", "SFTP", "WEBDAV", "WEBDAVS"]);
const DB_PROTOCOLS = new Set(["POSTGRES", "MYSQL", "MARIADB", "SQLSERVER", "ORACLE"]);
const DEFAULT_PORTS = {
  FTP: 21, FTPS: 21, SFTP: 22, WEBDAV: 80, WEBDAVS: 443,
  POSTGRES: 5432, MYSQL: 3306, MARIADB: 3306, SQLSERVER: 1433, ORACLE: 1521,
};
const REFRESH_MS = 10000;

const $ = (id) => document.getElementById(id);
let state = { overview: null, editingHasSecret: false };

/* ---------- API ---------- */
async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (res.status === 204) return null;
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = body && body.detail ? body.detail : `Error ${res.status}`;
    throw { status: res.status, detail };
  }
  return body;
}

/* ---------- Overview / tarjetas ---------- */
function fmtTs(iso) {
  if (!iso) return "—";
  const d = new Date(iso.endsWith("Z") ? iso : iso + "Z");
  return d.toLocaleString("es", { hour12: false });
}
function fmtPct(v) { return v == null ? "—" : v.toFixed(v === 100 ? 0 : 2) + "%"; }
function fmtMs(v) { return v == null ? "—" : Math.round(v) + " ms"; }
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function applyFilters(cards) {
  const q = $("f-search").value.trim().toLowerCase();
  const client = $("f-client").value;
  const proto = $("f-protocol").value;
  const status = $("f-status").value;
  return cards.filter((c) =>
    (!q || c.name.toLowerCase().includes(q)) &&
    (!client || c.client === client) &&
    (!proto || c.protocol === proto) &&
    (!status || c.status === status));
}

function renderOverview() {
  const data = state.overview;
  if (!data) return;

  const downs = data.connections.filter((c) => c.status === "DOWN");
  const degr = data.connections.filter((c) => c.status === "DEGRADED");
  const dot = $("global-dot");
  dot.className = "brand-dot " + (downs.length ? "down" : degr.length ? "degraded" : "up");
  $("global-summary").textContent =
    `${data.connections.length} conexiones · ${downs.length} caídas · ${degr.length} degradadas` +
    (data.paused ? " · MONITOREO PAUSADO" : "");
  $("btn-pause").textContent = data.paused ? "▶ Reanudar" : "⏸ Pausar todo";

  // Banner de incidentes abiertos (RF-4: banner persistente)
  const banner = $("incident-banner");
  const open = data.connections.filter((c) => c.open_incident);
  if (open.length) {
    banner.innerHTML = "⚠ Incidentes abiertos: " + open.map((c) =>
      `<b>${esc(c.name)}</b> (${esc(c.open_incident.error_type || "?")} desde ${fmtTs(c.open_incident.started_at)})`
    ).join(" · ");
    banner.classList.remove("hidden");
  } else banner.classList.add("hidden");

  // Filtro de clientes
  const sel = $("f-client");
  const current = sel.value;
  sel.innerHTML = '<option value="">Todos los clientes</option>' +
    data.clients.map((c) => `<option${c === current ? " selected" : ""}>${esc(c)}</option>`).join("");

  const cards = applyFilters(data.connections);
  $("empty-state").classList.toggle("hidden", data.connections.length > 0);
  $("cards").innerHTML = cards.map(cardHtml).join("");
  $("refresh-info").textContent = "Actualizado " + fmtTs(data.generated_at);
}

function cardHtml(c) {
  const status = c.status || "PENDIENTE";
  const error = c.status !== "UP" && c.last_error_msg
    ? `<div class="card-error">${esc(c.last_error_type || "")}: ${esc(c.last_error_msg)}</div>` : "";
  return `
  <div class="card status-${esc(c.status || "")}" data-id="${c.id}">
    <div class="card-head">
      <span class="pill ${esc(c.status || "")}">${esc(status)}</span>
      <b title="${esc(c.name)}">${esc(c.name)}</b>
      <span class="spacer"></span>
      ${c.client ? `<span class="chip">${esc(c.client)}</span>` : ""}
      <span class="chip">${esc(c.protocol)}</span>
    </div>
    <div class="muted">${esc(c.host)}:${c.port} · cada ${c.interval_s} s · último: ${fmtTs(c.last_check_ts)}</div>
    <div class="card-metrics">
      <div class="metric"><span class="v">${fmtPct(c.uptime.h24)}</span><span class="k">24 h</span></div>
      <div class="metric"><span class="v">${fmtPct(c.uptime.d7)}</span><span class="k">7 d</span></div>
      <div class="metric"><span class="v">${fmtPct(c.uptime.d30)}</span><span class="k">30 d</span></div>
      <div class="metric"><span class="v">${fmtMs(c.last_latency_ms)}</span><span class="k">latencia</span></div>
      <div class="metric"><span class="v">${fmtMs(c.avg_latency_ms)}</span><span class="k">prom. 24 h</span></div>
    </div>
    ${error}
    <div class="card-actions">
      <button class="btn small" data-act="detail">Detalle</button>
      <button class="btn small" data-act="edit">Editar</button>
      <button class="btn small" data-act="dup">Duplicar</button>
      <button class="btn small" data-act="toggle">${c.enabled ? "Pausar" : "Reanudar"}</button>
      <button class="btn small danger" data-act="del">Eliminar</button>
    </div>
  </div>`;
}

async function refresh() {
  try {
    state.overview = await api("/api/overview");
    renderOverview();
  } catch (e) {
    $("refresh-info").textContent = "Sin conexión con el servicio…";
  }
}

/* ---------- Formulario ---------- */
function protocolFields() {
  const p = $("c-protocol").value;
  const isDb = DB_PROTOCOLS.has(p);
  const isSftp = p === "SFTP";
  $("l-dbname").classList.toggle("hidden", !isDb);
  $("l-health").classList.toggle("hidden", !isDb);
  $("l-write").classList.toggle("hidden", isDb);
  $("l-auth").classList.toggle("hidden", !isSftp);
  $("l-keypath").classList.toggle("hidden", !(isSftp && $("c-auth").value === "key"));
  $("l-secret").firstChild.textContent =
    isSftp && $("c-auth").value === "key" ? "Passphrase de la llave " : "Contraseña ";
  $("c-port").placeholder = DEFAULT_PORTS[p] || "";
  $("c-targets").placeholder = isDb ? "ventas\nventas.pedidos" : "/clientes/acme/entrada";
}

function openForm(cfg) {
  $("form-title").textContent = cfg ? `Editar: ${cfg.name}` : "Nueva conexión";
  $("conn-form").reset();
  $("form-errors").classList.add("hidden");
  $("test-result").classList.add("hidden");
  state.editingHasSecret = !!(cfg && cfg.has_secret);
  $("c-id").value = cfg ? cfg.id : "";
  if (cfg) {
    $("c-name").value = cfg.name; $("c-client").value = cfg.client;
    $("c-protocol").value = cfg.protocol; $("c-host").value = cfg.host;
    $("c-port").value = cfg.port; $("c-username").value = cfg.username;
    $("c-auth").value = cfg.auth_type; $("c-keypath").value = cfg.key_path || "";
    $("c-dbname").value = cfg.db_name || ""; $("c-sslmode").value = cfg.ssl_mode;
    $("c-interval").value = cfg.interval_s; $("c-timeout").value = cfg.timeout_s;
    $("c-retries").value = cfg.retries; $("c-degraded").value = cfg.degraded_ms || "";
    $("c-targets").value = (cfg.targets || []).join("\n");
    $("c-health").value = cfg.health_query || ""; $("c-write").checked = cfg.write_check;
    $("c-notes").value = cfg.notes || "";
    $("c-secret").placeholder = cfg.has_secret ? "(sin cambios)" : "";
  } else {
    $("c-secret").placeholder = "";
  }
  protocolFields();
  $("modal-form").classList.remove("hidden");
}

function formPayload() {
  const secretRaw = $("c-secret").value;
  return {
    name: $("c-name").value,
    client: $("c-client").value,
    protocol: $("c-protocol").value,
    host: $("c-host").value,
    port: $("c-port").value ? parseInt($("c-port").value, 10) : null,
    username: $("c-username").value,
    secret: secretRaw === "" ? null : secretRaw, // null = conservar el guardado
    auth_type: $("c-auth").classList ? $("c-auth").value : "password",
    key_path: $("c-keypath").value || null,
    db_name: $("c-dbname").value || null,
    ssl_mode: $("c-sslmode").value,
    targets: $("c-targets").value.split("\n").map((s) => s.trim()).filter(Boolean),
    health_query: $("c-health").value || null,
    interval_s: parseInt($("c-interval").value || "60", 10),
    timeout_s: parseFloat($("c-timeout").value || "10"),
    retries: parseInt($("c-retries").value || "2", 10),
    degraded_ms: $("c-degraded").value ? parseInt($("c-degraded").value, 10) : null,
    write_check: $("c-write").checked,
    enabled: true,
    notes: $("c-notes").value,
  };
}

function showFormErrors(detail) {
  const box = $("form-errors");
  const items = Array.isArray(detail) ? detail : [String(detail)];
  box.innerHTML = "<ul>" + items.map((e) =>
    `<li>${esc(typeof e === "string" ? e : e.msg || JSON.stringify(e))}</li>`).join("") + "</ul>";
  box.classList.remove("hidden");
}

async function saveForm(ev) {
  ev.preventDefault();
  const id = $("c-id").value;
  const payload = formPayload();
  if (id) payload.enabled = undefined; // no tocar enabled al editar
  try {
    if (id) {
      const current = state.overview.connections.find((c) => c.id === parseInt(id, 10));
      payload.enabled = current ? current.enabled : true;
      await api(`/api/connections/${id}`, { method: "PUT", body: JSON.stringify(payload) });
    } else {
      await api("/api/connections", { method: "POST", body: JSON.stringify(payload) });
    }
    $("modal-form").classList.add("hidden");
    refresh();
  } catch (e) {
    showFormErrors(e.detail);
  }
}

async function testConnection() {
  const btn = $("btn-test");
  const box = $("test-result");
  btn.disabled = true; btn.textContent = "Probando…";
  box.classList.add("hidden");
  try {
    const payload = formPayload();
    if ($("c-id").value) payload.id = parseInt($("c-id").value, 10);
    const r = await api("/api/connections/test", { method: "POST", body: JSON.stringify(payload) });
    const ok = r.status === "UP";
    box.className = "test-result " + (ok ? "ok" : "bad");
    let html = `<b>${esc(r.status)}</b>`;
    if (r.latency_ms != null) html += ` · ${Math.round(r.latency_ms)} ms`;
    if (r.error_type) html += ` · ${esc(r.error_type)} — ${esc(r.error_msg)}`;
    if (r.targets && r.targets.length) {
      html += "<ul>" + r.targets.map((t) =>
        `<li>${t.ok ? "✔" : "✘"} ${esc(t.target)}${t.message ? " — " + esc(t.message) : ""}</li>`).join("") + "</ul>";
    }
    box.innerHTML = html;
  } catch (e) {
    box.className = "test-result bad";
    box.innerHTML = Array.isArray(e.detail)
      ? "<ul>" + e.detail.map((m) => `<li>${esc(typeof m === "string" ? m : m.msg)}</li>`).join("") + "</ul>"
      : esc(e.detail || "Error al probar");
  } finally {
    box.classList.remove("hidden");
    btn.disabled = false; btn.textContent = "Probar conexión";
  }
}

/* ---------- Detalle: gráficas (Chart.js local, specs dataviz) ---------- */
const DARK = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
const VIZ = {
  series: DARK ? "#3987e5" : "#2a78d6",
  grid: DARK ? "#2c2c2a" : "#e1e0d9",
  muted: "#898781",
  good: "#0ca30c", warning: DARK ? "#fab219" : "#eda100", critical: "#d03b3b",
  surface: DARK ? "#161d27" : "#ffffff",
};
let charts = { latency: null, availability: null };
let detailId = null;
let detailRange = "24h";

function fmtBucket(iso, range) {
  const d = new Date(iso.endsWith("Z") ? iso : iso + "Z");
  return range === "24h"
    ? d.toLocaleTimeString("es", { hour: "2-digit", minute: "2-digit", hour12: false })
    : d.toLocaleDateString("es", { day: "2-digit", month: "2-digit" }) +
      (range === "7d" ? " " + d.toLocaleTimeString("es", { hour: "2-digit", minute: "2-digit", hour12: false }) : "");
}

async function loadCharts() {
  if (detailId == null || typeof Chart === "undefined") return;
  const s = await api(`/api/connections/${detailId}/series?range=${detailRange}`);
  const axis = { grid: { color: VIZ.grid }, ticks: { color: VIZ.muted, maxTicksLimit: 10 }, border: { color: VIZ.grid } };

  if (charts.latency) charts.latency.destroy();
  charts.latency = new Chart($("chart-latency"), {
    type: "line",
    data: {
      labels: s.latency.map((p) => fmtBucket(p.t, detailRange)),
      datasets: [{
        label: "Latencia (ms)", data: s.latency.map((p) => p.ms),
        borderColor: VIZ.series, borderWidth: 2, pointRadius: 0, pointHitRadius: 12,
        pointHoverRadius: 4, pointHoverBackgroundColor: VIZ.series,
        pointHoverBorderColor: VIZ.surface, pointHoverBorderWidth: 2,
        tension: 0.15, spanGaps: false,
      }],
    },
    options: {
      animation: false, responsive: true, maintainAspectRatio: true,
      // Serie única: sin leyenda (el título de la sección la nombra)
      plugins: { legend: { display: false }, tooltip: { intersect: false, mode: "index" } },
      scales: { x: axis, y: { ...axis, beginAtZero: true } },
    },
  });

  if (charts.availability) charts.availability.destroy();
  const mk = (key, color, label) => ({
    label, data: s.availability.map((b) => b[key] || 0),
    backgroundColor: color, borderColor: VIZ.surface, borderWidth: 1,
    borderRadius: 2, stack: "a", maxBarThickness: 24,
  });
  charts.availability = new Chart($("chart-availability"), {
    type: "bar",
    data: {
      labels: s.availability.map((b) => fmtBucket(b.t, detailRange)),
      datasets: [
        mk("UP", VIZ.good, "UP"),
        mk("DEGRADED", VIZ.warning, "DEGRADED"),
        mk("DOWN", VIZ.critical, "DOWN"),
      ],
    },
    options: {
      animation: false, responsive: true,
      plugins: { legend: { display: true, labels: { color: VIZ.muted, boxWidth: 12 } } },
      scales: { x: { ...axis, stacked: true }, y: { ...axis, stacked: true, beginAtZero: true } },
    },
  });
}

/* ---------- Detalle ---------- */
function fmtDur(s) {
  if (s == null) return "—";
  if (s < 90) return Math.round(s) + " s";
  if (s < 5400) return (s / 60).toFixed(1) + " min";
  return (s / 3600).toFixed(1) + " h";
}

async function openDetail(id) {
  const card = state.overview.connections.find((c) => c.id === id);
  detailId = id;
  $("detail-title").textContent = card ? `${card.name} — ${card.host}:${card.port}` : "Detalle";
  $("detail-body").innerHTML = "Cargando…";
  $("btn-csv-checks").href = `/api/export/checks.csv?connection_id=${id}&days=30`;
  $("btn-csv-incidents").href = `/api/export/incidents.csv?connection_id=${id}`;
  $("modal-detail").classList.remove("hidden");
  loadCharts().catch(() => {});
  try {
    const h = await api(`/api/connections/${id}/history?hours=24`);
    const checks = h.checks.slice(-50).reverse();
    const incidents = h.incidents.slice(-20).reverse();
    $("detail-body").innerHTML = `
      <h2>Incidentes</h2>
      ${incidents.length ? `<table class="data"><tr><th>Inicio</th><th>Fin</th><th>Duración</th><th>Causa</th><th>Detalle</th></tr>
        ${incidents.map((i) => `<tr><td>${fmtTs(i.started_at)}</td><td>${i.ended_at ? fmtTs(i.ended_at) : "<b>abierto</b>"}</td>
          <td>${fmtDur(i.duration_s)}</td><td>${esc(i.error_type || "—")}</td><td>${esc(i.first_error_msg)}</td></tr>`).join("")}
      </table>` : '<p class="muted">Sin incidentes registrados.</p>'}
      <h2 style="margin-top:14px">Últimos chequeos (24 h)</h2>
      ${checks.length ? `<table class="data"><tr><th>Hora</th><th>Estado</th><th>Latencia</th><th>Causa</th></tr>
        ${checks.map((c) => `<tr><td>${fmtTs(c.ts_utc)}</td><td class="st-${esc(c.status)}">${esc(c.status)}</td>
          <td>${fmtMs(c.latency_ms)}</td><td>${esc(c.error_type ? c.error_type + " — " + c.error_msg : "")}</td></tr>`).join("")}
      </table>` : '<p class="muted">Aún no hay chequeos en las últimas 24 h.</p>'}`;
  } catch (e) {
    $("detail-body").innerHTML = '<p class="muted">No se pudo cargar el historial.</p>';
  }
}

/* ---------- Acciones de tarjeta ---------- */
async function cardAction(id, act) {
  if (act === "detail") return openDetail(id);
  if (act === "edit") {
    const cfg = await api(`/api/connections/${id}`);
    return openForm(cfg);
  }
  if (act === "dup") { await api(`/api/connections/${id}/duplicate`, { method: "POST" }); return refresh(); }
  if (act === "toggle") { await api(`/api/connections/${id}/toggle`, { method: "POST" }); return refresh(); }
  if (act === "del") {
    const card = state.overview.connections.find((c) => c.id === id);
    if (confirm(`¿Eliminar la conexión «${card ? card.name : id}» y todo su historial?`)) {
      await api(`/api/connections/${id}`, { method: "DELETE" });
      return refresh();
    }
  }
}

/* ---------- Reportes ---------- */
function reportDates() {
  const preset = $("r-preset").value;
  const today = new Date();
  const iso = (d) => d.toISOString().slice(0, 10);
  if (preset === "custom") return { date_from: $("r-from").value, date_to: $("r-to").value };
  if (preset === "prev-month") {
    const first = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth() - 1, 1));
    const last = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), 0));
    return { date_from: iso(first), date_to: iso(last) };
  }
  const days = parseInt(preset, 10);
  const from = new Date(today.getTime() - (days - 1) * 86400000);
  return { date_from: iso(from), date_to: iso(today) };
}

async function refreshReportList() {
  const items = await api("/api/reports");
  $("report-list").innerHTML = items.length
    ? items.map((r) =>
        `<div class="report-row"><a href="/reports/${encodeURIComponent(r.file)}" target="_blank">${esc(r.file)}</a>
         <span class="spacer"></span><span class="muted">${(r.size / 1024).toFixed(0)} KB</span></div>`).join("")
    : '<p class="muted">Aún no hay reportes generados.</p>';
}

async function openReports() {
  const clients = (state.overview && state.overview.clients) || [];
  $("r-client").innerHTML = clients.map((c) => `<option>${esc(c)}</option>`).join("") ||
    '<option value="">(sin clientes)</option>';
  $("report-errors").classList.add("hidden");
  $("modal-reports").classList.remove("hidden");
  refreshReportList().catch(() => {});
}

async function generateReport() {
  const btn = $("btn-generate-report");
  btn.disabled = true; btn.textContent = "Generando…";
  try {
    const body = { client: $("r-client").value, ...reportDates() };
    const r = await api("/api/reports", { method: "POST", body: JSON.stringify(body) });
    $("report-errors").classList.add("hidden");
    await refreshReportList();
    window.open(`/reports/${encodeURIComponent(r.file)}`, "_blank");
  } catch (e) {
    const box = $("report-errors");
    box.innerHTML = Array.isArray(e.detail) ? e.detail.map(esc).join("<br>") : esc(e.detail);
    box.classList.remove("hidden");
  } finally {
    btn.disabled = false; btn.textContent = "Generar reporte";
  }
}

/* ---------- Ajustes ---------- */
async function openSettings() {
  const data = await api("/api/settings");
  document.querySelectorAll("#settings-form [data-key]").forEach((el) => {
    const v = data[el.dataset.key];
    if (el.type === "checkbox") el.checked = v === "1";
    else if (el.dataset.key === "smtp.password") el.value = "";
    else el.value = v ?? "";
  });
  $("settings-errors").classList.add("hidden");
  $("modal-settings").classList.remove("hidden");
}

async function saveSettings(ev) {
  ev.preventDefault();
  const payload = {};
  document.querySelectorAll("#settings-form [data-key]").forEach((el) => {
    payload[el.dataset.key] = el.type === "checkbox" ? (el.checked ? "1" : "0") : el.value;
  });
  try {
    await api("/api/settings", { method: "PUT", body: JSON.stringify(payload) });
    $("modal-settings").classList.add("hidden");
  } catch (e) {
    const box = $("settings-errors");
    box.innerHTML = Array.isArray(e.detail)
      ? "<ul>" + e.detail.map((m) => `<li>${esc(m)}</li>`).join("") + "</ul>" : esc(e.detail);
    box.classList.remove("hidden");
  }
}

/* ---------- Wiring ---------- */
document.addEventListener("DOMContentLoaded", () => {
  $("btn-new").addEventListener("click", () => openForm(null));
  $("btn-reports").addEventListener("click", () => openReports().catch(() => {}));
  $("btn-reports-close").addEventListener("click", () => $("modal-reports").classList.add("hidden"));
  $("btn-generate-report").addEventListener("click", generateReport);
  $("r-preset").addEventListener("change", () => {
    document.querySelectorAll(".r-custom").forEach((el) =>
      el.classList.toggle("hidden", $("r-preset").value !== "custom"));
  });
  $("range-switch").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-range]");
    if (!btn) return;
    detailRange = btn.dataset.range;
    document.querySelectorAll("#range-switch .btn").forEach((b) =>
      b.classList.toggle("active", b === btn));
    loadCharts().catch(() => {});
  });
  $("btn-settings").addEventListener("click", () => openSettings().catch(() => {}));
  $("btn-settings-cancel").addEventListener("click", () => $("modal-settings").classList.add("hidden"));
  $("settings-form").addEventListener("submit", saveSettings);
  $("restore-file").addEventListener("change", async (ev) => {
    const file = ev.target.files[0];
    if (!file) return;
    try {
      const data = JSON.parse(await file.text());
      const r = await api("/api/restore", { method: "POST", body: JSON.stringify(data) });
      $("restore-result").textContent =
        `Creadas ${r.connections_created}, omitidas ${r.connections_skipped}. ${r.warning}`;
      refresh();
    } catch (e) {
      $("restore-result").textContent = "Error: " +
        (Array.isArray(e.detail) ? e.detail.join("; ") : (e.detail || "archivo inválido"));
    } finally {
      ev.target.value = "";
    }
  });
  $("btn-cancel").addEventListener("click", () => $("modal-form").classList.add("hidden"));
  $("btn-detail-close").addEventListener("click", () => $("modal-detail").classList.add("hidden"));
  $("conn-form").addEventListener("submit", saveForm);
  $("btn-test").addEventListener("click", testConnection);
  $("c-protocol").addEventListener("change", protocolFields);
  $("c-auth").addEventListener("change", protocolFields);
  $("btn-pause").addEventListener("click", async () => {
    await api(state.overview && state.overview.paused ? "/api/resume" : "/api/pause", { method: "POST" });
    refresh();
  });
  ["f-search", "f-client", "f-protocol", "f-status"].forEach((id) =>
    $(id).addEventListener("input", renderOverview));
  $("cards").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-act]");
    if (!btn) return;
    const id = parseInt(btn.closest(".card").dataset.id, 10);
    cardAction(id, btn.dataset.act).catch((e) => alert(
      Array.isArray(e.detail) ? e.detail.join("\n") : (e.detail || "Error")));
  });
  refresh();
  setInterval(refresh, REFRESH_MS);
});
