// Wires the live stream to the scene, header, event feed and panel.
import { renderControls, renderSite, renderStation } from "./panel.js";
import { buildScene, updateScene } from "./scene.js";

const svg = document.getElementById("scene");
const live = document.getElementById("live");
const details = document.getElementById("details");
const controls = document.getElementById("controls");
let meta = null;
let snapshot = null;
let selected = null;
let builtFor = "";
let holdUntil = 0; // pause panel re-render while the user drags a slider
let activeTab = "details"; // "#controls" in the URL opens the control panel

function selectionFromHash() {
  const m = location.hash.match(/^#(site|station)=(.+)$/);
  return m ? { type: m[1], id: decodeURIComponent(m[2]) } : null;
}
selected = selectionFromHash();
if (location.hash === "#controls") activeTab = "controls";
window.addEventListener("hashchange", () => { selected = selectionFromHash(); render(); });

const ctx = {
  select(sel) {
    selected = sel;
    history.replaceState(null, "", `${location.search}#${sel.type}=${encodeURIComponent(sel.id)}`);
    switchTab("details");
    render();
  },
  hold() { holdUntil = Date.now() + 8000; },
  release() { holdUntil = 0; },
};

function switchTab(tab) {
  activeTab = tab;
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  details.hidden = tab !== "details";
  controls.hidden = tab !== "controls";
}
document.querySelectorAll(".tabs button").forEach((b) => { b.onclick = () => { switchTab(b.dataset.tab); render(); }; });

function setKpi(name, ratio, text) {
  const k = document.querySelector(`.kpi[data-kpi="${name}"]`);
  const len = 2 * Math.PI * 18;
  const r = Math.max(0, Math.min(1, ratio ?? 0));
  k.querySelector(".value").style.strokeDasharray = `${(r * len).toFixed(1)} ${len.toFixed(1)}`;
  k.querySelector("b").textContent = text;
}

function renderHeader() {
  const k = snapshot.kpis;
  const capTotal = snapshot.sites.reduce((a, s) => a + s.cap_w, 0);
  setKpi("availability", k.availability, k.availability == null ? "–" : `${Math.round(k.availability * 100)}%`);
  setKpi("online", k.stations_total ? k.stations_online / k.stations_total : 0, `${k.stations_online}/${k.stations_total}`);
  setKpi("charging", k.connectors_total ? k.charging / k.connectors_total : 0, `${k.charging}`);
  setKpi("power", capTotal ? k.power_w / capTotal : 0, `${(k.power_w / 1000).toFixed(0)} kW`);
  setKpi("faulted", k.connectors_total ? k.faulted / k.connectors_total : 0, `${k.faulted}`);
  const errs = Object.keys(snapshot.upstream_errors || {});
  live.className = `live ${errs.length ? "err" : "ok"}`;
  live.textContent = errs.length ? `degraded: ${errs.join(", ")}` : "live";
  const banner = document.getElementById("banner");
  const run = snapshot.scenario?.running;
  const parts = [];
  if (run) parts.push(`Scenario running: ${run.title} (${Math.round(run.elapsed_s)}/${run.duration_s}\u00a0s)`);
  if (snapshot.world?.enabled) parts.push("Real-world mode on");
  banner.hidden = !parts.length;
  banner.textContent = parts.join(" · ");
}

function renderFeed() {
  const feed = document.getElementById("feed");
  const events = snapshot.events || [];
  if (!events.length) return;
  feed.innerHTML = events.slice(0, 20).map((e) => {
    const t = new Date(e.time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const who = e.station || e.site || "";
    return `<li class="${e.level}"><time>${t}</time><span><b>${who}</b> ${e.text}</span></li>`;
  }).join("");
}

function renderPanel() {
  if (Date.now() < holdUntil) return;
  if (activeTab === "controls") { renderControls(controls, snapshot, meta); return; }
  if (!selected) return;
  if (selected.type === "site") {
    const site = snapshot.sites.find((s) => s.code === selected.id);
    if (site) renderSite(details, site, snapshot, ctx);
  } else {
    const st = snapshot.stations.find((s) => s.id === selected.id);
    const site = st && meta.sites.find((s) => s.code === st.site);
    if (st) renderStation(details, st, site, meta, ctx);
  }
}

function render() {
  if (!snapshot) return;
  const key = snapshot.stations.map((s) => s.id).join(",");
  if (key && key !== builtFor) {
    buildScene(svg, meta.sites, snapshot.stations, ctx.select);
    builtFor = key;
  }
  updateScene(snapshot, selected);
  renderHeader();
  renderFeed();
  renderPanel();
}

async function start() {
  meta = await (await fetch("/api/meta")).json();
  snapshot = await (await fetch("/api/state")).json();
  switchTab(activeTab);
  render();
  // ?static renders one snapshot without the live stream (used for screenshots).
  if (new URLSearchParams(location.search).has("static")) return;
  const es = new EventSource("/api/stream");
  es.onmessage = (msg) => { snapshot = JSON.parse(msg.data); render(); };
  es.onerror = () => { live.className = "live err"; live.textContent = "reconnecting…"; };
}

start().catch((err) => { live.className = "live err"; live.textContent = `failed: ${err.message}`; });
