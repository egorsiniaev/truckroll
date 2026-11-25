// Details and control panel. Renders from the latest snapshot; actions POST to the control API.
import { STATE_COLORS, loadClass } from "./scene.js";

const NB = " ";
export const kw = (w) => `${(w / 1000).toFixed(1)}${NB}kW`;
const kwh = (wh) => `${(wh / 1000).toFixed(1)}${NB}kWh`;
const pct = (x) => (x == null ? "–" : `${Math.round(x * 100)}%`);
// Uptime lives in the high 90s; whole percent would show 99.6 % as 100 %.
const upct = (x) => (x == null ? "–" : `${(x * 100).toFixed(x >= 0.995 && x < 1 ? 2 : 1)}%`);
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const LATENCIES = [0, 500, 2000, 5000];
const STATUS_CLASS = { Available: "ok", Preparing: "warn", Finishing: "warn", Charging: "info", SuspendedEV: "info",
  SuspendedEVSE: "info", Faulted: "bad", Unavailable: "bad" };
const TRANSITIONAL = new Set(["Preparing", "Finishing"]);
const STUCK_WARN_S = 60;
const STUCK_ALERT_S = 180; // matches the incident rule

function since(seconds) {
  if (seconds == null) return "";
  return seconds >= 3600 ? `${Math.floor(seconds / 3600)}${NB}h ${Math.round((seconds % 3600) / 60)}${NB}min`
    : seconds >= 60 ? `${Math.round(seconds / 60)}${NB}min` : `${seconds}${NB}s`;
}

function doneAt(eta) {
  if (eta == null) return "–";
  const t = new Date(Date.now() + eta * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return `${t} <span class="muted">(${since(eta)})</span>`;
}

function connectorRow(c) {
  const age = c.status_age_s;
  const stuck = TRANSITIONAL.has(c.status) && age >= STUCK_ALERT_S ? "bad" : TRANSITIONAL.has(c.status) && age >= STUCK_WARN_S ? "warn" : "";
  const sess = c.session;
  const session = sess ? `${sess.delivered_kwh} / ${sess.need_kwh}${NB}kWh${sess.adopted ? ' <span class="muted" title="taken over after a restart; target is an estimate">*</span>' : ""}`
    : c.session_wh != null ? kwh(c.session_wh) : "–";
  const done = sess?.starting ? '<span class="muted">starting…</span>' : doneAt(sess?.eta_s);
  const err = c.error_code !== "NoError" ? `<br><span class="bad">${esc(c.error_code)}</span>` : "";
  return `<tr><td>${c.id}</td><td><span class="st ${STATUS_CLASS[c.status] ?? ""}">${esc(c.status)}</span>
    <br><span class="age ${stuck}">for ${since(age) || "–"}${stuck === "bad" ? " · stuck" : ""}</span>${err}</td>
    <td>${kw(c.power_w)}</td><td>${session}</td><td>${done}</td></tr>`;
}

const STATUS_HELP = "Statuses are OCPP 1.6: Available = free; Preparing = car plugged in, session starting, no energy yet; "
  + "Charging = energy flowing; Suspended = paused by the car or the charger; Finishing = session ended, car still "
  + "plugged in; Faulted = error, can't charge.";
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

const BASE_TARIFF = { depot: 0.32, retail: 0.49, highway: 0.69, office: 0.39 };

function tariffNote(site, money) {
  const base = BASE_TARIFF[site.profile];
  if (!base || money.tariff_eur_kwh == null) return "";
  const d = money.tariff_eur_kwh / base - 1;
  if (Math.abs(d) < 0.005) return `<span class="muted">(base)</span>`;
  return d > 0 ? `<span class="warn">(+${Math.round(d * 100)}%, price spike)</span>` : `<span class="ok">(−${Math.round(-d * 100)}%, cheap power or&nbsp;solar)</span>`;
}

function batterySection(b) {
  if (!b) return "";
  const cls = b.mode === "charging" ? "info" : b.mode === "discharging" ? "warn" : "muted";
  return `<h4>Battery</h4>
    <div class="grid2">${stat(`${Math.round(b.soc * 100)}%`, `${b.soc_kwh} of ${b.capacity_kwh}${NB}kWh stored`)}
    ${stat(`<i class="val ${cls}">${b.mode === "charging" ? "+" : ""}${(b.power_w / 1000).toFixed(0)}${NB}kW</i>`, `${esc(b.mode)}, max ${b.power_kw_max}${NB}kW`)}</div>
    <p class="meta">${esc(b.reason)}. Charges from spare solar and&nbsp;from the&nbsp;grid in&nbsp;the&nbsp;day’s cheapest quarter (only within the&nbsp;grid cap); supplies the&nbsp;site when power is&nbsp;expensive or&nbsp;demand goes above the&nbsp;cap.</p>`;
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
      ${stat(upct(site.uptime_today), `uptime since ${snapshot.kpis.uptime_since || "–"} (time-weighted)`)}
    </div>
    <div class="bar"><i class="${loadClass(site.cap_used)}" style="width:${Math.min(100, used * 100).toFixed(1)}%"></i></div>
    ${ctl?.active ? `<p class="meta">Site controller active: chargers share the cap at ${kw(ctl.share_w)} each, so sessions take longer.</p>` : ""}
    <h4>Demand</h4>
    <p class="meta">${esc(dem.reason ?? "")}: ${dem.arrivals_per_hour ?? 0} cars/hour coming${dem.price_factor && dem.price_factor !== 1 ? ` (${dem.price_factor > 1 ? "+" : "−"}${Math.round(Math.abs(dem.price_factor - 1) * 100)}% because of the tariff)` : ""}.
      The app shows a&nbsp;wait of&nbsp;${mins(dem.expected_wait_s ?? 0)}. Today: ${dem.completed ?? 0} done, ${dem.diverted ?? 0} saw the wait and&nbsp;went elsewhere, ${dem.abandoned ?? 0} gave up in&nbsp;the&nbsp;queue, ${dem.interrupted ?? 0} interrupted${dem.avg_wait_s ? `; average wait ${mins(dem.avg_wait_s)}` : ""}.</p>
    ${batterySection(site.battery)}
    <h4>Money today</h4>
    <table class="money">
      <tr><td>Tariff now ${tariffNote(site, money)}</td><td>€${(money.tariff_eur_kwh ?? 0).toFixed(2)}/kWh</td></tr>
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
    <div class="row"><div class="what"><b class="${site.network.enabled ? (site.network.latency_ms ? "warn" : "ok") : "bad"}">${site.network.enabled ? "Connected" : "Outage: site can't reach the backend"}</b><span class="${site.network.latency_ms ? "warn" : ""}">${site.network.latency_ms ? `+${site.network.latency_ms}${NB}ms added latency` : "no added latency"}</span></div>
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
    ${incidents.map((i) => `<p class="meta bad">Open incident: ${esc(i.title)}</p>`).join("")}
    <div class="grid2">
      ${stat(kw(st.power_w), "power now")}
      ${stat(st.limit_w == null ? "none" : kw(st.limit_w), "power limit")}
      ${stat(`${st.heartbeat_age_s}${NB}s`, "since last message")}
      ${stat(`<i class="val ${st.connected ? "ok" : "bad"}">${st.connected ? "online" : "offline"}</i>`, "connection")}
    </div>
    ${sources.length ? `<p class="meta">Limited by: ${sources.map(([k, v]) => `${esc(k)} ${kw(v)}`).join(", ")} (lowest wins)</p>` : ""}
    <h4>Connectors</h4>
    <table><tr><th>#</th><th>Status</th><th title="Charging power right now, from the charger's meter">Power</th>
      <th title="Energy delivered in this session / what the car needs">Session</th><th title="When the car should be done at the current power">Done</th></tr>
      ${st.connectors.map((c) => connectorRow(c)).join("")}
    </table>
    <p class="meta">${STATUS_HELP}</p>
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

function agentRow(a) {
  if (!a) return `<div class="row"><div class="what"><b>AI triage agent</b><span>off: no Gemini key configured</span></div></div>`;
  const cost = a.free_tier ? `free tier (would be $${a.cost_usd_if_paid.toFixed(3)} paid)` : `$${a.cost_usd_if_paid.toFixed(3)} today`;
  const state = a.paused_s ? `paused ${mins(a.paused_s)}: ${esc(a.last_error ?? "")}` : a.last_error ? esc(a.last_error) : `${esc(a.model)}`;
  return `<div class="row"><div class="what"><b>AI triage agent</b><span>${a.calls_today}/${a.daily_cap} calls today · ${(a.tokens_in / 1000).toFixed(1)}k in, ${(a.tokens_out / 1000).toFixed(1)}k out · ${cost} · ${state}</span></div>
    <button class="switch ${a.enabled ? "on" : ""}" data-act="agent" aria-label="AI triage agent"></button></div>`;
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
    <div class="row"><div class="what"><b>Human approval</b><span>risky responses wait for you; no answer in&nbsp;time means the&nbsp;default</span></div>
      <button class="switch ${snapshot.decisions?.enabled ? "on" : ""}" data-act="hitl" aria-label="Human approval"></button></div>
    ${agentRow(snapshot.agent)}
    <div class="row"><div class="what"><b>Random scenarios</b><span>${ap.enabled ? (run ? "waiting for the current one to end" : `next in ${mins(ap.next_in_s)}`) : "off"}</span></div>
      <button class="switch ${ap.enabled ? "on" : ""}" data-act="autopilot" aria-label="Random scenarios"></button></div>
    <div class="row"><div class="what"><b>Dynamic pricing</b><span>public tariffs follow price spikes; dearer chargers lose drivers to&nbsp;competitors</span></div>
      <button class="switch ${snapshot.economics?.dynamic ? "on" : ""}" data-act="pricing" aria-label="Dynamic pricing"></button></div>
    <div class="row"><div class="what"><b>Site batteries</b><span>store cheap and&nbsp;solar power, release it in&nbsp;spikes and&nbsp;above the&nbsp;grid cap</span></div>
      <button class="switch ${snapshot.batteries_enabled ? "on" : ""}" data-act="batteries" aria-label="Site batteries"></button></div>
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
    <table class="chaos">${snapshot.sites.map((s) => `<tr><td><b class="${s.network.enabled ? (s.network.latency_ms ? "warn" : "ok") : "bad"}">${esc(s.code)}</b></td>
      <td><button class="mini ${s.network.enabled ? "danger" : "primary"}" data-outage="${esc(s.code)}" data-on="${s.network.enabled}">${s.network.enabled ? "Cut" : "Restore"}</button></td>
      <td><select data-lat="${esc(s.code)}">${LATENCIES.map((ms) => `<option value="${ms}" ${ms === s.network.latency_ms ? "selected" : ""}>${ms ? `+${ms}${NB}ms` : "no latency"}</option>`).join("")}</select></td></tr>`).join("")}
    </table>
    <div class="control"><button class="mini" data-act="clear-chaos">Restore all networks</button></div>`;
  root.querySelector('[data-act="world"]').onclick = (e) => post("/api/world", { enabled: !world.enabled }, e.target);
  const agentBtn = root.querySelector('[data-act="agent"]');
  if (agentBtn) agentBtn.onclick = (e) => post("/api/agent", { enabled: !snapshot.agent?.enabled }, e.target);
  root.querySelector('[data-act="hitl"]').onclick = (e) => post("/api/hitl", { enabled: !snapshot.decisions?.enabled }, e.target);
  root.querySelector('[data-act="batteries"]').onclick = (e) => post("/api/batteries", { enabled: !snapshot.batteries_enabled }, e.target);
  root.querySelector('[data-act="pricing"]').onclick = (e) => post("/api/pricing", { enabled: !snapshot.economics?.dynamic }, e.target);
  root.querySelector('[data-act="autopilot"]').onclick = (e) => post("/api/autopilot", { enabled: !ap.enabled }, e.target);
  root.querySelectorAll("[data-scenario]").forEach((b) => { b.onclick = () => post(`/api/scenarios/${b.dataset.scenario}/start`, {}, b); });
  root.querySelectorAll("[data-outage]").forEach((b) => { b.onclick = () => post(`/api/sites/${b.dataset.outage}/outage`, { on: b.dataset.on === "true" }, b); });
  root.querySelectorAll("[data-lat]").forEach((sel) => { sel.onchange = () => post(`/api/sites/${sel.dataset.lat}/latency`, { ms: Number(sel.value) }, sel); });
  root.querySelector('[data-act="clear-chaos"]').onclick = (e) => post("/api/chaos/clear", {}, e.target);
  const stop = root.querySelector('[data-act="stop-scenario"]');
  if (stop) stop.onclick = (e) => post("/api/scenarios/stop", {}, e.target);
}


const KPI_INFO = {
  availability: {
    title: "Uptime today",
    what: "Share of connector-time that was usable since local midnight (or since the control service started). "
      + "Usable = station connected and connector free, preparing, charging, suspended or finishing. Faulted, "
      + "unavailable and offline time is downtime. This is how charging uptime is reported; US NEVI asks for 97 % or more.",
    rows: (s) => s.sites.map((x) => [x.code, upct(x.uptime_today), `${x.downtime_min ?? 0}${NB}connector-min down`]),
    value: (s) => `${((s.kpis.uptime_today ?? 0) * 100).toFixed(2)}% since ${s.kpis.uptime_since || "–"}`,
  },
  charging: {
    title: "Charging now",
    what: "Connectors with energy flowing to a car right now (OCPP Charging or Suspended), out of all connectors.",
    rows: (s) => s.sites.map((x) => [x.code, `${x.states?.charging ?? 0} stations charging`, `${x.free}/${x.connectors_total} free`]),
    value: (s) => `${s.kpis.charging} of ${s.kpis.connectors_total} connectors`,
  },
  queue: {
    title: "Cars waiting",
    what: "Drivers queued for a free connector. They give up after their patience runs out (minutes at retail and "
      + "the highway, hours at the depot); that is lost revenue. Queues grow when demand is high or a grid cap "
      + "slows charging.",
    rows: (s) => s.sites.map((x) => [x.code, `${x.demand?.queue ?? 0} waiting`, `${x.demand?.abandoned ?? 0} gave up today`]),
    value: (s) => `${s.kpis.queue ?? 0} cars`,
  },
  power: {
    title: "Power vs grid caps",
    what: "Total charging power against the sum of the sites' grid connection caps. Below 30 % green, 30-70 % "
      + "yellow, 70 % and up red. Above 100 % at a site, the site controller limits chargers.",
    rows: (s) => s.sites.map((x) => [x.code, `${(x.power_w / 1000).toFixed(0)} of ${(x.cap_w / 1000).toFixed(0)}${NB}kW`, pct(x.cap_used)]),
    value: (s) => `${(s.kpis.power_w / 1000).toFixed(0)}${NB}kW`,
  },
  price: {
    title: "Power price",
    what: "Where the number comes from: the ENTSO-E day-ahead market for the Netherlands, one price per 15 minutes, "
      + "fixed the day before. What it costs us: that price plus about €0.11/kWh in network fees and taxes; solar "
      + "and battery energy are free at the moment of use. What drivers pay: each site has a base tariff (depot "
      + "€0.32 and office €0.39 on contracts, retail €0.49, highway €0.69). With dynamic pricing, public tariffs "
      + "follow the market: 60 % of a spike above today's average is added (up to +60 %), and 80 % of a dip below "
      + "it is given back as a discount (down to −40 %), plus 10 % off while solar covers half the site. How it "
      + "changes demand: price-sensitive drivers (retail, highway) come less when it's dear and more when it's "
      + "cheap, up to 1.8x; they also check the wait in an app and go elsewhere if it's long. What else reacts: "
      + "in the day's top 20 % the depot fleet charges at half power and the batteries supply the sites; in the "
      + "cheapest quarter the batteries fill up.",
    rows: (s) => s.sites.map((x) => [x.code, `tariff €${(x.economics?.tariff_eur_kwh ?? 0).toFixed(2)}/kWh`,
      `cost €${(x.economics?.buy_eur_kwh ?? 0).toFixed(2)}/kWh`, x.battery ? `battery ${x.battery.mode}` : ""]),
    value: (s) => (s.prices?.available ? `€${s.prices.now_eur_mwh.toFixed(0)}/MWh now · today €${s.prices.min_eur_mwh.toFixed(0)} to €${s.prices.max_eur_mwh.toFixed(0)}, average €${s.prices.avg_eur_mwh.toFixed(0)}${s.prices.expensive ? " · expensive slot" : s.prices.rank <= 0.25 ? " · cheap slot" : ""}` : "no data"),
    extra: (s) => sparkline(s.prices),
  },
  margin: {
    title: "Margin today",
    what: "Revenue from charging minus the cost of grid energy, since local midnight (or service start). Solar is free.",
    rows: (s) => s.sites.map((x) => [x.code, eur(x.economics?.margin_eur), `lost ${eur(x.economics?.lost_eur)}`]),
    value: (s) => eur(s.economics?.fleet?.margin_eur),
  },
  incidents: {
    title: "Open incidents",
    what: "Problems the system detected and is handling: what happened, the automatic response, and recovery. "
      + "Click an incident in the strip above the map for its timeline.",
    rows: (s) => (s.incidents || []).filter((i) => i.status !== "recovered").map((i) => [i.site, i.title, i.status]),
    value: (s) => `${(s.incidents || []).filter((i) => i.status === "open").length} open`,
  },
};

export function renderKpi(root, id, snapshot) {
  const info = KPI_INFO[id];
  if (!info) return;
  const rows = info.rows(snapshot);
  root.innerHTML = `
    <h3>${esc(info.title)}</h3>
    <p class="kpi-value">${esc(info.value(snapshot))}</p>
    ${info.extra ? info.extra(snapshot) : ""}
    <p>${esc(info.what)}</p>
    ${rows.length ? `<table>${rows.map((r) => `<tr>${r.map((c) => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")}</table>` : `<p class="hint">Nothing right now.</p>`}`;
}


const secs = (v) => (v >= 3600 ? `${(v / 3600).toFixed(1)}${NB}h` : v >= 60 ? `${Math.round(v / 60)}${NB}min` : `${v}${NB}s`);
const SLO_FMT = { ratio: (v) => `${(v * 100).toFixed(v >= 0.995 && v < 1 ? 2 : 1)}%`, count: (v) => `${v}`, seconds: secs };
const SLO_TARGET = { ratio: (v, hb) => `${hb ? "≥" : "≤"} ${(v * 100).toFixed(0)}%`, count: (v) => `${v}`, seconds: (v) => `≤ ${secs(v)}` };
const RESPONSE_IDS = new Set(["ttd", "ttm", "ttr_remote", "ttr_field", "answered"]);

function sloRows(rows) {
  return `<table class="slo">${rows.map((r) => `<tr class="${r.status.replace(" ", "-")}">
      <td><b>${esc(r.name)}</b><br><span class="muted">${esc(r.detail)}</span></td>
      <td class="v">${r.value == null ? "–" : SLO_FMT[r.unit](r.value)}</td>
      <td class="t">target ${SLO_TARGET[r.unit](r.target, !["cap"].includes(r.id))}</td>
      <td><span class="slo-pill">${esc(r.status)}</span></td></tr>`).join("")}</table>`;
}

export function renderSlo(root, snapshot) {
  const rows = snapshot.slo || [];
  root.innerHTML = `
    <h3>Service levels today</h3>
    <p class="meta">Since ${esc(snapshot.kpis?.uptime_since || "midnight")}. The targets are from GOALS.md.</p>
    ${sloRows(rows.filter((r) => !RESPONSE_IDS.has(r.id)))}
    <h4>Incident response</h4>
    ${sloRows(rows.filter((r) => RESPONSE_IDS.has(r.id)))}
    ${(snapshot.field_jobs || []).length ? `<h4>Technicians out</h4><ul class="rules">${snapshot.field_jobs.map((j) => `<li>🚐 ${esc(j.station)}: ${j.status === "driving" ? "on the way" : "on site"}, done in ${secs(j.in_s)}</li>`).join("")}</ul>` : ""}
    <h4>AI agent eval</h4>
    <div id="eval-box">${evalHtml(lastEval)}</div>
    <p class="hint">Click a&nbsp;site, a&nbsp;charger, a&nbsp;metric or&nbsp;an&nbsp;incident for details. Click empty map to&nbsp;come back here.</p>`;
  refreshEval(root);
}

let lastEval = null;
let evalAt = 0;
function refreshEval(root) {
  if (Date.now() - evalAt < 30_000) return;
  evalAt = Date.now();
  fetch("/api/eval").then((r) => (r.ok ? r.json() : null)).then((e) => {
    lastEval = e;
    const box = root.querySelector("#eval-box");
    if (box) box.innerHTML = evalHtml(e);
  }).catch(() => {});
}

function evalHtml(e) {
  if (!e) return `<p class="hint">No eval yet.</p>`;
  const p = (x) => (x == null ? "–" : `${Math.round(x * 100)}%`);
  return `<table class="slo">
    <tr class="${e.accuracy == null ? "" : e.accuracy >= 0.8 ? "met" : "missed"}"><td><b>Root cause right</b><br><span class="muted">any triage in&nbsp;the&nbsp;run, of ${e.runs_triaged} triaged runs (target ≥ 80%); first triage only: ${p(e.accuracy_first)}</span></td><td class="v">${p(e.accuracy)}</td></tr>
    <tr class="${e.false_all_clear ? "missed" : "met"}"><td><b>False “all clear”</b><br><span class="muted">said nothing to do about a real fault (target 0)</span></td><td class="v">${e.false_all_clear}</td></tr>
    <tr class="${e.median_seconds_to_summary == null ? "" : e.median_seconds_to_summary <= 120 ? "met" : "missed"}"><td><b>Time to summary</b><br><span class="muted">median, from incident open (target &lt; 2&nbsp;min)</span></td><td class="v">${e.median_seconds_to_summary == null ? "–" : `${Math.round(e.median_seconds_to_summary)}${NB}s`}</td></tr>
    <tr><td><b>Cost per triage</b><br><span class="muted">average tokens in / out</span></td><td class="v">${e.avg_tokens_in ?? "–"} / ${e.avg_tokens_out ?? "–"}</td></tr>
    </table><p class="meta">${e.runs} scenario runs scored since the agent went live (${e.runs_before_agent} earlier runs not scored); ${e.triages_total} triages, ${e.triages_outside_scenarios} outside scenarios. Small samples move a lot.</p>`;
}
