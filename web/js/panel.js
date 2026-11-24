// Details and control panel. Renders from the latest snapshot; actions POST to the control API.
import { STATE_COLORS, loadClass } from "./scene.js";

const NB = " ";
export const kw = (w) => `${(w / 1000).toFixed(1)}${NB}kW`;
const kwh = (wh) => `${(wh / 1000).toFixed(1)}${NB}kWh`;
const pct = (x) => (x == null ? "–" : `${Math.round(x * 100)}%`);
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const LATENCIES = [0, 500, 2000, 5000];
const eur = (v) => `${v < 0 ? "−" : ""}€${Math.abs(v ?? 0).toFixed(2)}`;
const mins = (s) => (s == null ? "–" : s >= 60 ? `${Math.round(s / 60)}${NB}min` : `${Math.round(s)}${NB}s`);

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

const pill = (state) => `<span class="pill" style="background:${STATE_COLORS[state] ?? "#94a3b8"}">${esc(state)}</span>`;
const stat = (value, label) => `<div class="stat"><b>${value}</b><span>${label}</span></div>`;

function rulesList(rules) {
  if (!rules?.length) return `<p class="hint">No real-world rules for&nbsp;this site.</p>`;
  return `<ul class="rules">${rules.map((r) => `<li class="${r.active ? "active" : "muted"}">${r.active ? "● active:" : "○ standby:"} ${esc(r.text)}</li>`).join("")}</ul>`;
}

export function renderSite(root, site, snapshot, ctx) {
  const w = site.weather;
  const stations = snapshot.stations.filter((s) => s.site === site.code);
  const used = site.cap_used ?? 0;
  const ctl = site.controller;
  const dem = site.demand || {};
  const money = site.economics || {};
  root.innerHTML = `
    <h3>${esc(site.code)}</h3>
    <p class="meta">${esc(site.kind)} · ${site.stations} stations · ${site.stations * site.connectors} connectors</p>
    <div class="grid2">
      ${stat(kw(site.power_w), `of ${kw(site.cap_w)} grid cap (${Math.round(used * 100)}%)`)}
      ${stat(`${site.free}/${site.connectors_total}`, "connectors free now")}
      ${stat(`${dem.queue ?? 0}`, dem.queue ? `waiting, longest ${mins(dem.longest_wait_s)}` : "cars waiting")}
      ${stat(pct(site.availability), "uptime (working connectors)")}
    </div>
    <div class="bar"><i class="${loadClass(site.cap_used)}" style="width:${Math.min(100, used * 100).toFixed(1)}%"></i></div>
    ${ctl?.active ? `<p class="meta">Site controller active: chargers share the cap at ${kw(ctl.share_w)} each, so sessions take longer.</p>` : ""}
    <h4>Demand</h4>
    <p class="meta">${dem.arrivals_per_hour ?? 0} cars/hour arriving now · ${dem.completed ?? 0} done today · ${dem.abandoned ?? 0} gave up waiting · ${dem.interrupted ?? 0} interrupted${dem.avg_wait_s ? ` · average wait ${mins(dem.avg_wait_s)}` : ""}</p>
    <h4>Money today</h4>
    <table class="money">
      <tr><td>Tariff now</td><td>€${(money.tariff_eur_kwh ?? 0).toFixed(2)}/kWh</td></tr>
      <tr><td>Energy cost now (spot + fees)</td><td>€${(money.buy_eur_kwh ?? 0).toFixed(2)}/kWh</td></tr>
      <tr><td>Sold ${(money.energy_kwh ?? 0).toFixed(0)}${NB}kWh (${(money.solar_kwh ?? 0).toFixed(0)} from solar)</td><td>${eur(money.revenue_eur)}</td></tr>
      <tr><td>Bought ${(money.grid_kwh ?? 0).toFixed(0)}${NB}kWh from the grid</td><td>${eur(-(money.cost_eur ?? 0))}</td></tr>
      <tr class="total"><td>Margin</td><td class="${(money.margin_eur ?? 0) < 0 ? "neg" : "pos"}">${eur(money.margin_eur)}</td></tr>
      <tr><td class="muted">Lost to waits and interruptions</td><td class="muted">${eur(money.lost_eur)}</td></tr>
    </table>
    <h4>Real-world rules here</h4>
    ${rulesList(site.rules)}
    <h4>Weather and&nbsp;solar now</h4>
    ${w ? `<div class="grid2">
      ${stat(`${w.temp_c ?? "–"}°C`, "temperature")}
      ${stat(`${w.gust_kmh ?? "–"}${NB}km/h`, "wind gusts")}
      ${stat(kw(site.solar_w ?? 0), `solar from ${kw(site.pv_w ?? 0)} panels`)}
      ${stat(kw(Math.max(0, site.power_w - (site.solar_w ?? 0))), "drawn from the grid")}
    </div><p class="meta">Open-Meteo, ${esc(w.time ?? "")}${w.stale ? " (stale)" : ""}</p>` : `<p class="hint">No weather yet.</p>`}
    <h4>Network</h4>
    <div class="row"><div class="what"><b>${site.network.enabled ? "Connected" : "Outage"}</b><span>${site.network.latency_ms ? `+${site.network.latency_ms}${NB}ms added latency` : "no added latency"}</span></div>
      <button class="mini ${site.network.enabled ? "danger" : "primary"}" data-act="outage">${site.network.enabled ? "Cut network" : "Restore"}</button></div>
    <h4>Stations</h4>
    <div class="chips">${stations.map((s) => `<button class="chip" data-station="${esc(s.id)}"><i class="dot ${s.state}"></i>${esc(s.id.replace(`${site.code}-`, ""))}</button>`).join("")}</div>
    ${snapshot.grafana_url ? `<p class="meta" style="margin-top:12px"><a href="${esc(snapshot.grafana_url)}" target="_blank" rel="noopener">See ${esc(site.code)} in&nbsp;Grafana ↗</a></p>` : ""}`;
  root.querySelector('[data-act="outage"]').onclick = (e) => post(`/api/sites/${site.code}/outage`, { on: site.network.enabled }, e.target);
  root.querySelectorAll("[data-station]").forEach((b) => { b.onclick = () => ctx.select({ type: "station", id: b.dataset.station }); });
}

export function renderStation(root, st, site, meta, snapshot, ctx) {
  const maxW = site ? site.power_w : 22000;
  const limit = st.limit_w ?? maxW;
  const sources = Object.entries(st.limit_sources || {});
  const incidents = (snapshot.incidents || []).filter((i) => i.stations.includes(st.id) && i.status === "open");
  root.innerHTML = `
    <p class="meta"><a href="#" data-site>${esc(st.site)}</a></p>
    <h3>${esc(st.id)} ${pill(st.state)}</h3>
    <p class="meta">${esc(st.vendor)} ${esc(st.model)} · firmware ${esc(st.firmware || "–")}</p>
    ${incidents.map((i) => `<p class="meta" style="color:#b91c1c">Open incident: ${esc(i.title)}</p>`).join("")}
    <div class="grid2">
      ${stat(kw(st.power_w), "power now")}
      ${stat(st.limit_w == null ? "none" : kw(st.limit_w), "power limit")}
      ${stat(`${st.heartbeat_age_s}${NB}s`, "since last message")}
      ${stat(st.connected ? "online" : "offline", "connection")}
    </div>
    ${sources.length ? `<p class="meta">Limited by: ${sources.map(([k, v]) => `${esc(k)} ${kw(v)}`).join(", ")} (lowest wins)</p>` : ""}
    <h4>Connectors</h4>
    <table><tr><th>#</th><th>Status</th><th>Power</th><th>Energy</th><th>Error</th></tr>
      ${st.connectors.map((c) => `<tr><td>${c.id}</td><td>${esc(c.status)}</td><td>${kw(c.power_w)}</td><td>${kwh(c.energy_wh)}</td><td>${c.error_code === "NoError" ? "–" : esc(c.error_code)}</td></tr>`).join("")}
    </table>
    <h4>Control</h4>
    <div class="control">
      <label for="limit">Operator power limit (OCPP smart-charging profile)</label>
      <input id="limit" type="range" min="0" max="${maxW}" step="${maxW > 50000 ? 5000 : 1000}" value="${limit}">
      <output id="limit-out">${kw(limit)}</output>
      <button class="mini primary" data-act="limit">Apply</button>
      <button class="mini" data-act="clear-limit">Clear</button>
    </div>
    <div class="control">
      <select id="fault">${meta.error_codes.map((c) => `<option>${esc(c)}</option>`).join("")}</select>
      <button class="mini danger" data-act="fault">Inject fault</button>
      <button class="mini" data-act="clear-fault">Clear fault</button>
    </div>
    <div class="control">
      <button class="mini" data-act="off">Switch off</button>
      <button class="mini" data-act="on">Switch on</button>
      <button class="mini" data-act="reset">Soft reset</button>
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

function sparkline(prices) {
  const curve = prices?.curve || [];
  if (curve.length < 2) return "";
  const W = 320, H = 46;
  const vals = curve.map((c) => c.p);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const x = (i) => (i / (curve.length - 1)) * W;
  const y = (v) => H - 4 - ((v - lo) / Math.max(1, hi - lo)) * (H - 8);
  const d = curve.map((c, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(c.p).toFixed(1)}`).join("");
  const now = new Date();
  const idx = Math.min(curve.length - 1, Math.floor(((now.getHours() * 60 + now.getMinutes()) / 1440) * curve.length));
  const hot = y(prices.expensive_from_eur_mwh);
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <rect class="hot" x="0" y="0" width="${W}" height="${Math.max(0, hot).toFixed(1)}"></rect>
    <path class="area" d="${d}L${W},${H}L0,${H}Z"></path><path d="${d}"></path>
    <line class="now" x1="${x(idx).toFixed(1)}" x2="${x(idx).toFixed(1)}" y1="0" y2="${H}"></line></svg>`;
}

function fleetMoney(snapshot) {
  const f = snapshot.economics?.fleet;
  if (!f) return "";
  return `<div class="row"><div class="what"><b>Today: ${eur(f.margin_eur)} margin</b>
    <span>${eur(f.revenue_eur)} sold, ${eur(f.cost_eur)} energy cost, ${eur(f.lost_eur)} lost to waits</span></div></div>`;
}

export function renderControls(root, snapshot, meta) {
  const world = snapshot.world || {};
  const ap = snapshot.autopilot || {};
  const run = snapshot.scenario?.running;
  const prices = snapshot.prices || {};
  const activeRules = (world.rules || []).filter((r) => r.active);
  const progress = run ? Math.min(100, (run.elapsed_s / run.duration_s) * 100) : 0;
  root.innerHTML = `
    <div class="row"><div class="what"><b>Real-world mode</b><span>local time sets demand, weather and&nbsp;prices set limits</span></div>
      <button class="switch ${world.enabled ? "on" : ""}" data-act="world" aria-label="Real-world mode"></button></div>
    ${world.enabled && activeRules.length ? `<ul class="active-rules">${activeRules.map((r) => `<li>● ${esc(r.site)}: ${esc(r.text)}</li>`).join("")}</ul>` : ""}
    <div class="row"><div class="what"><b>Random scenarios</b><span>${ap.enabled ? (run ? "waiting for the current one to end" : `next in ${mins(ap.next_in_s)}`) : "off"}</span></div>
      <button class="switch ${ap.enabled ? "on" : ""}" data-act="autopilot" aria-label="Random scenarios"></button></div>
    <div class="row"><div class="what"><b>Dynamic pricing</b><span>public tariffs follow price spikes; dearer chargers lose drivers to&nbsp;competitors</span></div>
      <button class="switch ${snapshot.economics?.dynamic ? "on" : ""}" data-act="pricing" aria-label="Dynamic pricing"></button></div>
    ${fleetMoney(snapshot)}
    <h4>Power price (ENTSO-E, NL day-ahead)</h4>
    ${prices.available ? `<div class="row"><div class="what"><b>€${prices.now_eur_mwh.toFixed(0)}/MWh now${prices.expensive ? " · expensive" : ""}</b>
      <span>today €${prices.min_eur_mwh.toFixed(0)} to €${prices.max_eur_mwh.toFixed(0)}, red band = top 20%</span></div></div>${sparkline(prices)}`
      : `<p class="hint">No price data${snapshot.upstream_errors?.prices ? `: ${esc(snapshot.upstream_errors.prices)}` : ""}.</p>`}
    <h4>Scenarios</h4>
    ${run ? `<div class="running"><b>${esc(run.icon ?? "")} ${esc(run.title)}</b> <span class="muted">${Math.round(run.elapsed_s)}/${run.duration_s}${NB}s</span>
      <div class="bar"><i style="width:${progress.toFixed(0)}%"></i></div>
      <button class="mini danger" data-act="stop-scenario" style="margin-top:6px">Stop and&nbsp;clean up</button></div>` : ""}
    ${(meta.scenarios || []).map((s) => `<div class="row scen ${run?.id === s.id ? "is-running" : ""}" title="${esc(s.description)}"><span class="scen-icon" aria-hidden="true">${esc(s.icon ?? "⚠️")}</span><div class="what"><b>${esc(s.title)}</b><span>${esc(s.description)}</span></div>
      <button class="mini" data-scenario="${esc(s.id)}" ${run ? "disabled" : ""}>Start</button></div>`).join("")}
    <h4>Network per site</h4>
    <table class="chaos">${snapshot.sites.map((s) => `<tr><td><b>${esc(s.code)}</b></td>
      <td><button class="mini ${s.network.enabled ? "danger" : "primary"}" data-outage="${esc(s.code)}" data-on="${s.network.enabled}">${s.network.enabled ? "Cut" : "Restore"}</button></td>
      <td><select data-lat="${esc(s.code)}">${LATENCIES.map((ms) => `<option value="${ms}" ${ms === s.network.latency_ms ? "selected" : ""}>${ms ? `+${ms}${NB}ms` : "no latency"}</option>`).join("")}</select></td></tr>`).join("")}
    </table>
    <div class="control"><button class="mini" data-act="clear-chaos">Restore all networks</button></div>`;
  root.querySelector('[data-act="world"]').onclick = (e) => post("/api/world", { enabled: !world.enabled }, e.target);
  root.querySelector('[data-act="pricing"]').onclick = (e) => post("/api/pricing", { enabled: !snapshot.economics?.dynamic }, e.target);
  root.querySelector('[data-act="autopilot"]').onclick = (e) => post("/api/autopilot", { enabled: !ap.enabled }, e.target);
  root.querySelectorAll("[data-scenario]").forEach((b) => { b.onclick = () => post(`/api/scenarios/${b.dataset.scenario}/start`, {}, b); });
  root.querySelectorAll("[data-outage]").forEach((b) => { b.onclick = () => post(`/api/sites/${b.dataset.outage}/outage`, { on: b.dataset.on === "true" }, b); });
  root.querySelectorAll("[data-lat]").forEach((sel) => { sel.onchange = () => post(`/api/sites/${sel.dataset.lat}/latency`, { ms: Number(sel.value) }, sel); });
  root.querySelector('[data-act="clear-chaos"]').onclick = (e) => post("/api/chaos/clear", {}, e.target);
  const stop = root.querySelector('[data-act="stop-scenario"]');
  if (stop) stop.onclick = (e) => post("/api/scenarios/stop", {}, e.target);
}
