// Details and control panel. Renders from the latest snapshot; actions POST to the control API.
import { STATE_COLORS } from "./scene.js";

const NB = " ";
const kw = (w) => `${(w / 1000).toFixed(1)}${NB}kW`;
const kwh = (wh) => `${(wh / 1000).toFixed(1)}${NB}kWh`;
const pct = (x) => (x == null ? "–" : `${Math.round(x * 100)}%`);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const LATENCIES = [0, 500, 2000, 5000];

let toastTimer = null;
export function toast(text, isError = false) {
  const t = document.getElementById("toast");
  t.textContent = text;
  t.classList.toggle("err", isError);
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 3500);
}

export async function post(path, body, button) {
  if (button) button.disabled = true;
  try {
    const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `HTTP ${res.status}`);
    toast("Done");
    return data;
  } catch (err) {
    toast(`Failed: ${err.message}`, true);
    return null;
  } finally {
    if (button) button.disabled = false;
  }
}

function pill(state) {
  return `<span class="pill" style="background:${STATE_COLORS[state] ?? "#94a3b8"}">${esc(state)}</span>`;
}

function stat(value, label) {
  return `<div class="stat"><b>${value}</b><span>${label}</span></div>`;
}

export function renderSite(root, site, snapshot, ctx) {
  const w = site.weather;
  const stations = snapshot.stations.filter((s) => s.site === site.code);
  const used = site.cap_used ?? 0;
  root.innerHTML = `
    <h3>${esc(site.code)}</h3>
    <p class="meta">${esc(site.kind)} · ${site.stations} stations · ${site.stations * site.connectors} connectors</p>
    <div class="grid2">
      ${stat(kw(site.power_w), `of ${kw(site.cap_w)} grid cap`)}
      ${stat(pct(site.availability), "connectors available")}
    </div>
    <div class="bar"><i class="${used > 1 ? "over" : ""}" style="width:${Math.min(100, used * 100).toFixed(1)}%"></i></div>
    <h4>Weather and&nbsp;solar now</h4>
    ${w ? `<div class="grid2">
      ${stat(`${w.temp_c ?? "–"}°C`, "temperature")}
      ${stat(`${w.gust_kmh ?? "–"}${NB}km/h`, "wind gusts")}
      ${stat(`${w.cloud_pct ?? "–"}%`, "cloud cover")}
      ${stat(`${w.radiation_wm2 ?? "–"}${NB}W/m²`, "solar radiation")}
      ${stat(kw(site.solar_w ?? 0), `solar from ${kw(site.pv_w ?? 0)} panels`)}
      ${stat(kw(Math.max(0, site.power_w - (site.solar_w ?? 0))), "drawn from the grid")}
    </div><p class="meta">Open-Meteo, ${esc(w.time ?? "")}${w.stale ? " (stale)" : ""}</p>` : `<p class="hint">No weather yet.</p>`}
    <h4>Network</h4>
    <p>${site.network.enabled ? "Connected" : "<b style='color:#b91c1c'>Outage</b>"}${site.network.latency_ms ? `, +${site.network.latency_ms}${NB}ms latency` : ""}</p>
    <div class="control">
      <button class="act ${site.network.enabled ? "danger" : "primary"}" data-act="outage">${site.network.enabled ? "Start network outage" : "Restore network"}</button>
    </div>
    <div class="control">
      <label for="lat">Added latency</label>
      <select id="lat">${LATENCIES.map((ms) => `<option value="${ms}" ${ms === site.network.latency_ms ? "selected" : ""}>${ms ? `${ms}${NB}ms` : "none"}</option>`).join("")}</select>
      <button class="act" data-act="latency">Apply</button>
    </div>
    <h4>Stations</h4>
    <div class="chips">${stations.map((s) => `<button class="chip" data-station="${esc(s.id)}"><i class="dot ${s.state}"></i>${esc(s.id.replace(`${site.code}-`, ""))}</button>`).join("")}</div>`;
  root.querySelector('[data-act="outage"]').onclick = (e) => post(`/api/sites/${site.code}/outage`, { on: site.network.enabled }, e.target);
  root.querySelector('[data-act="latency"]').onclick = (e) => post(`/api/sites/${site.code}/latency`, { ms: Number(root.querySelector("#lat").value) }, e.target);
  root.querySelectorAll("[data-station]").forEach((b) => { b.onclick = () => ctx.select({ type: "station", id: b.dataset.station }); });
}

export function renderStation(root, st, site, meta, ctx) {
  const maxW = site ? site.power_w : 22000;
  const limit = st.limit_w ?? maxW;
  root.innerHTML = `
    <p class="meta"><a href="#" data-site>${esc(st.site)}</a></p>
    <h3>${esc(st.id)} ${pill(st.state)}</h3>
    <p class="meta">${esc(st.vendor)} ${esc(st.model)} · firmware ${esc(st.firmware || "–")}</p>
    <div class="grid2">
      ${stat(kw(st.power_w), "power now")}
      ${stat(st.limit_w == null ? "none" : kw(st.limit_w), "power limit")}
      ${stat(`${st.heartbeat_age_s}${NB}s`, "since last message")}
      ${stat(st.connected ? "online" : "offline", "connection")}
    </div>
    <h4>Connectors</h4>
    <table><tr><th>#</th><th>Status</th><th>Power</th><th>Energy</th><th>Error</th></tr>
      ${st.connectors.map((c) => `<tr><td>${c.id}</td><td>${esc(c.status)}</td><td>${kw(c.power_w)}</td><td>${kwh(c.energy_wh)}</td><td>${c.error_code === "NoError" ? "–" : esc(c.error_code)}</td></tr>`).join("")}
    </table>
    <h4>Control</h4>
    <div class="control">
      <label for="limit">Power limit (sent as OCPP smart-charging profile)</label>
      <input id="limit" type="range" min="0" max="${maxW}" step="${maxW > 50000 ? 5000 : 1000}" value="${limit}">
      <output id="limit-out">${kw(limit)}</output>
      <button class="act primary" data-act="limit">Apply</button>
      <button class="act" data-act="clear-limit">Clear</button>
    </div>
    <div class="control">
      <label for="fault">Inject a&nbsp;fault on&nbsp;every connector</label>
      <select id="fault">${meta.error_codes.map((c) => `<option>${esc(c)}</option>`).join("")}</select>
      <button class="act danger" data-act="fault">Inject</button>
      <button class="act" data-act="clear-fault">Clear fault</button>
    </div>
    <div class="control">
      <button class="act" data-act="off">Switch off</button>
      <button class="act" data-act="on">Switch on</button>
      <button class="act" data-act="reset">Soft reset</button>
    </div>`;
  const range = root.querySelector("#limit");
  const out = root.querySelector("#limit-out");
  range.oninput = () => { out.textContent = kw(Number(range.value)); ctx.hold(); };
  const base = `/api/stations/${encodeURIComponent(st.id)}`;
  const on = (act, fn) => { root.querySelector(`[data-act="${act}"]`).onclick = (e) => { ctx.release(); fn(e.target); }; };
  on("limit", (b) => post(`${base}/limit`, { watts: Number(range.value) }, b));
  on("clear-limit", (b) => post(`${base}/limit`, { watts: null }, b));
  on("fault", (b) => post(`${base}/fault`, { error_code: root.querySelector("#fault").value }, b));
  on("clear-fault", (b) => post(`${base}/clear-fault`, {}, b));
  on("off", (b) => post(`${base}/power`, { on: false }, b));
  on("on", (b) => post(`${base}/power`, { on: true }, b));
  on("reset", (b) => post(`${base}/reset`, {}, b));
  root.querySelector("[data-site]").onclick = (e) => { e.preventDefault(); ctx.select({ type: "site", id: st.site }); };
}

function worldSection(snapshot) {
  const w = snapshot.world || { enabled: false, rules: [], demand: {} };
  const rules = (w.rules || []).map((r) => `<li class="${r.active ? "" : "muted"}">${r.active ? "●" : "○"} <b>${esc(r.site)}</b> ${esc(r.text)}</li>`).join("");
  const demand = Object.entries(w.demand || {}).map(([site, share]) => `${esc(site)} ${Math.round(share * 100)}%`).join(" · ");
  return `
    <h4 style="margin-top:0">Real-world mode</h4>
    <p class="hint">When on, local time sets how busy each site is and&nbsp;live weather from Open-Meteo triggers derates and&nbsp;network trouble. Local hour: ${w.hour ?? "–"}.</p>
    <div class="control"><button class="act ${w.enabled ? "" : "primary"}" data-act="world">${w.enabled ? "Turn real-world mode off" : "Turn real-world mode on"}</button></div>
    ${w.enabled && demand ? `<p class="meta">Arrivals now: ${demand}</p>` : ""}
    <ul class="rules">${rules}</ul>`;
}

function scenarioSection(snapshot, meta) {
  const run = snapshot.scenario?.running;
  const list = (meta.scenarios || []).map((s) => `
    <div class="site-row">
      <h5>${esc(s.title)} <span class="muted">~${Math.round(s.duration_s / 60)}${NB}min</span></h5>
      <p class="meta">${esc(s.description)}</p>
      <button class="act" data-scenario="${esc(s.id)}" ${run ? "disabled" : ""}>Start</button>
    </div>`).join("");
  const progress = run ? Math.min(100, (run.elapsed_s / run.duration_s) * 100) : 0;
  return `
    <h4>Scenarios</h4>
    ${run ? `<div class="running"><b>Running: ${esc(run.title)}</b>
      <div class="bar"><i style="width:${progress.toFixed(0)}%"></i></div>
      <ul class="rules">${run.steps_done.map((t) => `<li>✓ ${esc(t)}</li>`).join("")}</ul>
      <button class="act danger" data-act="stop-scenario">Stop and&nbsp;clean up</button></div>` : ""}
    ${list}`;
}

export function renderControls(root, snapshot, meta) {
  root.innerHTML = `
    ${worldSection(snapshot)}
    ${scenarioSection(snapshot, meta)}
    <h4>Network chaos per site</h4>
    ${snapshot.sites.map((s) => `
      <div class="site-row">
        <h5>${esc(s.code)} <span class="muted">${s.network.enabled ? "connected" : "OUTAGE"}${s.network.latency_ms ? ` · +${s.network.latency_ms}${NB}ms` : ""}</span></h5>
        <div class="control">
          <button class="act ${s.network.enabled ? "danger" : "primary"}" data-outage="${esc(s.code)}" data-on="${s.network.enabled}">${s.network.enabled ? "Outage" : "Restore"}</button>
          ${LATENCIES.map((ms) => `<button class="act" data-lat="${esc(s.code)}" data-ms="${ms}">${ms ? `+${ms}${NB}ms` : "No latency"}</button>`).join("")}
        </div>
      </div>`).join("")}
    <div class="control" style="margin-top:14px">
      <button class="act primary" data-act="clear-chaos">Clear all network chaos</button>
    </div>
    <h4>Stations</h4>
    <p class="hint">Power limits, faults, switch off and&nbsp;reset live in&nbsp;each station’s details. Click a&nbsp;charger on&nbsp;the&nbsp;map.</p>`;
  root.querySelectorAll("[data-outage]").forEach((b) => { b.onclick = () => post(`/api/sites/${b.dataset.outage}/outage`, { on: b.dataset.on === "true" }, b); });
  root.querySelectorAll("[data-lat]").forEach((b) => { b.onclick = () => post(`/api/sites/${b.dataset.lat}/latency`, { ms: Number(b.dataset.ms) }, b); });
  root.querySelector('[data-act="clear-chaos"]').onclick = (e) => post("/api/chaos/clear", {}, e.target);
  root.querySelector('[data-act="world"]').onclick = (e) => post("/api/world", { enabled: !snapshot.world?.enabled }, e.target);
  root.querySelectorAll("[data-scenario]").forEach((b) => { b.onclick = () => post(`/api/scenarios/${b.dataset.scenario}/start`, {}, b); });
  const stop = root.querySelector('[data-act="stop-scenario"]');
  if (stop) stop.onclick = (e) => post("/api/scenarios/stop", {}, e.target);
}
