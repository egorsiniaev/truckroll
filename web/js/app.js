// Wires the live stream to the scene, header, incident feed and panel.
import { esc, post, renderControls, renderKpi, renderSite, renderSlo, renderStation } from "./panel.js";
import { buildScene, screenPos, updateScene } from "./scene.js";

const NB = " ";
const svg = document.getElementById("scene");
const live = document.getElementById("live");
const details = document.getElementById("details");
const controls = document.getElementById("controls");
const feedBox = document.getElementById("feed-box");
let meta = null;
let snapshot = null;
let selected = null;
let builtFor = "";
let holdUntil = 0; // pause panel re-render while the user drags a slider
let activeTab = "details"; // "#controls" in the URL opens the control panel
let expanded = null; // incident id whose timeline is open
let showLog = false;

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

// Pan and zoom: drag the map, wheel or buttons to zoom. A drag never counts as a click.
const HOME = { x: -800, y: -700, w: 1600, h: 1180 }; // room at the top for the incidents strip
let vb = { ...HOME };
const applyView = () => { svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`); renderCallouts(); };
applyView();
const unitsPerPx = () => Math.max(vb.w / svg.clientWidth, vb.h / svg.clientHeight);
let drag = null;
let suppressClick = false;
svg.addEventListener("pointerdown", (e) => { drag = { x: e.clientX, y: e.clientY, vb: { ...vb }, moved: false }; });
window.addEventListener("pointermove", (e) => {
  if (!drag) return;
  const dx = e.clientX - drag.x;
  const dy = e.clientY - drag.y;
  if (!drag.moved && Math.hypot(dx, dy) < 4) return;
  drag.moved = true;
  svg.classList.add("dragging");
  const k = unitsPerPx();
  vb = { ...vb, x: drag.vb.x - dx * k, y: drag.vb.y - dy * k };
  applyView();
});
window.addEventListener("pointerup", () => {
  if (drag?.moved) suppressClick = true;
  drag = null;
  svg.classList.remove("dragging");
});
svg.addEventListener("click", (e) => { if (suppressClick) { e.stopPropagation(); e.preventDefault(); suppressClick = false; } }, true);
// A click on empty map clears the selection.
svg.addEventListener("click", (e) => {
  if (e.target.closest(".site, .station, .label-card")) return;
  if (!selected) return;
  selected = null;
  history.replaceState(null, "", location.search || location.pathname);
  render();
});
// Clicking a KPI chip explains it.
document.querySelectorAll(".kpi").forEach((k) => {
  k.classList.add("clickable");
  k.onclick = () => { selected = { type: "kpi", id: k.dataset.kpi }; switchTab("details"); render(); };
});
function zoom(factor, cx, cy) {
  const w = Math.min(4000, Math.max(400, vb.w * factor));
  const h = w * (vb.h / vb.w);
  const px = cx ?? vb.x + vb.w / 2;
  const py = cy ?? vb.y + vb.h / 2;
  vb = { x: px - (px - vb.x) * (w / vb.w), y: py - (py - vb.y) * (h / vb.h), w, h };
  applyView();
}
svg.addEventListener("wheel", (e) => {
  e.preventDefault();
  const pt = new DOMPoint(e.clientX, e.clientY).matrixTransform(svg.getScreenCTM().inverse());
  zoom(e.deltaY > 0 ? 1.1 : 1 / 1.1, pt.x, pt.y);
}, { passive: false });
document.getElementById("zoom-in").onclick = () => zoom(1 / 1.25);
document.getElementById("zoom-out").onclick = () => zoom(1.25);
document.getElementById("zoom-home").onclick = () => { vb = { ...HOME }; applyView(); };

// Incidents panel: collapsed state survives reloads.
if (localStorage.getItem("feedCollapsed") === "1") feedBox.classList.add("collapsed");
document.getElementById("feed-toggle").onclick = () => {
  const collapsed = feedBox.classList.toggle("collapsed");
  localStorage.setItem("feedCollapsed", collapsed ? "1" : "0");
  document.getElementById("feed-toggle").setAttribute("aria-expanded", String(!collapsed));
};
document.getElementById("log-toggle").onclick = (e) => {
  showLog = !showLog;
  e.target.textContent = showLog ? "Hide raw log" : "Show raw log";
  document.getElementById("log").hidden = !showLog;
  renderFeed();
};

function setKpi(name, ratio, text, alarm = false) {
  const k = document.querySelector(`.kpi[data-kpi="${name}"]`);
  const len = 2 * Math.PI * 18;
  const r = Math.max(0, Math.min(1, ratio ?? 0));
  k.querySelector(".value").style.strokeDasharray = `${(r * len).toFixed(1)} ${len.toFixed(1)}`;
  k.querySelector("b").textContent = text;
  k.classList.toggle("alarm", alarm);
}

const PHASE_LABEL = { morning: "Morning", day: "Day", evening: "Evening", night: "Night" };
const forcedPhase = new URLSearchParams(location.search).get("phase"); // preview a phase: ?phase=night

function renderHeader() {
  const k = snapshot.kpis;
  const capTotal = snapshot.sites.reduce((a, s) => a + s.cap_w, 0);
  const up = k.uptime_today;
  setKpi("availability", up, up == null ? "–" : `${(up * 100).toFixed(up < 0.995 ? 1 : 2)}%`, (up ?? 1) < 0.97);
  setKpi("charging", k.connectors_total ? k.charging / k.connectors_total : 0, `${k.charging}/${k.connectors_total}`);
  setKpi("queue", Math.min(1, (k.queue ?? 0) / 10), `${k.queue ?? 0}`, (k.queue ?? 0) >= 5);
  const load = capTotal ? k.power_w / capTotal : 0;
  setKpi("power", load, `${(k.power_w / 1000).toFixed(0)}${NB}kW`);
  document.querySelector('.kpi[data-kpi="power"]').dataset.load = load >= 0.7 ? "high" : load >= 0.3 ? "mid" : "low";
  const money = snapshot.economics?.fleet;
  if (money) setKpi("margin", money.revenue_eur ? Math.max(0, money.margin_eur / money.revenue_eur) : 0, `€${money.margin_eur.toFixed(0)}`, money.margin_eur < 0);
  const p = snapshot.prices || {};
  setKpi("price", p.available ? p.rank : 0, p.available ? `€${p.now_eur_mwh.toFixed(0)}/MWh` : "–", !!p.expensive);
  const open = (snapshot.incidents || []).filter((i) => i.status === "open").length;
  const asks = snapshot.decisions?.pending?.length ?? 0;
  setKpi("incidents", Math.min(1, open / 5), asks ? `${open} · ${asks} ask${asks > 1 ? "" : "s"} you` : `${open}`, open > 0 || asks > 0);

  const d = snapshot.daylight;
  if (d) {
    const phase = forcedPhase || d.phase;
    document.body.className = `phase-${phase}`;
    const sun = phase === "night" || phase === "morning" ? (d.sunrise ? `sunrise ${d.sunrise}` : "") : (d.sunset ? `sunset ${d.sunset}` : "");
    document.getElementById("clock").innerHTML = `${esc(d.date)}, ${esc(d.local_time)} Amsterdam <span class="phase-pill">${PHASE_LABEL[phase] ?? phase}${sun ? ` · ${esc(sun)}` : ""}</span>`;
  }
  const errs = Object.keys(snapshot.upstream_errors || {});
  live.className = `live ${errs.length ? "err" : "ok"}`;
  live.textContent = errs.length ? `degraded: ${errs.join(", ")}` : "live";
  const banner = document.getElementById("banner");
  const run = snapshot.scenario?.running;
  banner.hidden = !run;
  if (run) banner.textContent = `${run.icon ?? ""} Scenario: ${run.title} (${Math.round(run.elapsed_s)}/${run.duration_s}${NB}s)`;
  if (snapshot.grafana_url) document.getElementById("grafana").href = snapshot.grafana_url;
}

const fmtTime = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const ACTOR = { system: "", "auto-rule": "auto rule", operator: "operator", scenario: "scenario", agent: "AI agent" };
const STEP = { detected: "What", response: "Response", update: "Update", recovered: "Recovered", triage: "Agent", decision: "Asks" };

function questionHtml(d, compact = false) {
  const def = d.default === "approve" ? "yes" : "no";
  return `<div class="q-text"><b>${esc(d.question)}</b>${compact ? "" : ` <span class="muted">${esc(d.why)}</span>`}</div>
    <div class="q-actions"><button class="mini primary" data-dec="${esc(d.id)}" data-choice="approve">Approve</button>
    <button class="mini" data-dec="${esc(d.id)}" data-choice="reject">Reject</button>
    <span class="q-clock" title="No answer means ${def}">${d.seconds_left}${NB}s → ${def}</span></div>`;
}

function wireDecisionButtons(root) {
  root.querySelectorAll("[data-dec]").forEach((b) => {
    b.onclick = (e) => { e.stopPropagation(); post(`/api/decisions/${b.dataset.dec}`, { choice: b.dataset.choice }, b); };
  });
}

function renderQuestions() {
  const pending = snapshot.decisions?.pending ?? [];
  const list = document.getElementById("questions");
  list.innerHTML = pending.length ? `<li class="q-head">Waiting for you</li>` + pending.map((d) =>
    `<li class="question">${questionHtml(d)}</li>`).join("") : "";
  wireDecisionButtons(list);
}

function renderCallouts() {
  const box = document.getElementById("callouts");
  if (!box || !snapshot) return;
  const stage = box.parentElement.getBoundingClientRect();
  const pending = (snapshot.decisions?.pending ?? []).slice(0, 4);
  box.innerHTML = pending.map((d) => {
    const station = d.stations?.length === 1 ? d.stations[0] : null;
    const pos = station ? screenPos(svg, "station", station) : screenPos(svg, "site", d.site === "ALL" ? snapshot.sites[0]?.code : d.site);
    if (!pos) return "";
    return `<div class="callout" style="left:${(pos.x - stage.left).toFixed(0)}px;top:${(pos.y - stage.top).toFixed(0)}px">${questionHtml(d, true)}</div>`;
  }).join("");
  wireDecisionButtons(box);
}

function incidentAge(i) {
  if (i.status === "recovered") return `fixed in ${Math.max(1, Math.round(i.ttr_s / 60))}${NB}min`;
  return `${i.status === "monitoring" ? "watching · " : ""}${Math.max(0, Math.round(i.age_s / 60))}${NB}min`;
}

function renderFeed() {
  const incidents = snapshot.incidents || [];
  const open = incidents.filter((i) => i.status === "open");
  const watching = incidents.filter((i) => i.status === "monitoring");
  const parts = [open.length && `${open.length} open`, watching.length && `${watching.length} monitoring`].filter(Boolean);
  document.getElementById("feed-summary").textContent = parts.length ? parts.join(", ") : incidents.length ? "all recovered" : "";
  const list = document.getElementById("incidents");
  if (!incidents.length) {
    list.innerHTML = `<li class="muted">No incidents yet. Random scenarios start one every 5 to&nbsp;10&nbsp;minutes.</li>`;
  } else {
    // One line per incident: what, the chain of steps, how long. Click a line for the full timeline.
    list.innerHTML = incidents.slice(0, 12).map((i) => {
      const chain = i.timeline.map((t) => `<span class="step ${t.type}">${esc(t.short ?? t.type)}</span>`).join('<span class="arrow">→</span>');
      const steps = expanded === i.id ? `<ol class="steps">${i.timeline.map((t) => `<li class="${t.type}" title="${esc(t.text)}">
          <time>${fmtTime(t.time)}</time><b>${STEP[t.type] ?? t.type}</b><span>${esc(t.text)}</span><em>${ACTOR[t.actor] ?? ""}</em></li>`).join("")}</ol>` : "";
      return `<li class="inc-row ${i.severity} ${i.status} ${expanded === i.id ? "open" : ""}" data-inc="${esc(i.id)}">
        <div class="line"><i class="sev"></i><time class="opened">${new Date(i.opened).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time><b class="title">${esc(i.title)}</b><span class="chain">${chain}</span><small>${incidentAge(i)}</small></div>${steps}</li>`;
    }).join("");
    list.querySelectorAll("[data-inc] > .line").forEach((line) => {
      line.onclick = () => {
        const inc = incidents.find((x) => x.id === line.parentElement.dataset.inc);
        expanded = expanded === inc.id ? null : inc.id;
        if (inc.stations.length === 1) ctx.select({ type: "station", id: inc.stations[0] });
        else ctx.select({ type: "site", id: inc.site });
      };
    });
  }
  if (showLog) {
    document.getElementById("log").innerHTML = (snapshot.events || []).slice(0, 40).map((e) =>
      `<li><time>${fmtTime(e.time)}</time><span><b>${esc(e.station || e.site || "")}</b> ${esc(e.text)}</span></li>`).join("");
  }
}

function controlsBusy() {
  const a = document.activeElement;
  return a && controls.contains(a) && (a.tagName === "SELECT" || a.tagName === "INPUT");
}

function renderPanel() {
  if (Date.now() < holdUntil) return;
  if (activeTab === "controls") { if (!controlsBusy()) renderControls(controls, snapshot, meta); return; }
  if (!selected) { renderSlo(details, snapshot); return; }
  if (selected.type === "kpi") { renderKpi(details, selected.id, snapshot); return; }
  if (selected.type === "site") {
    const site = snapshot.sites.find((s) => s.code === selected.id);
    if (site) renderSite(details, site, snapshot, ctx);
  } else {
    const st = snapshot.stations.find((s) => s.id === selected.id);
    const site = st && meta.sites.find((s) => s.code === st.site);
    if (st) renderStation(details, st, site, meta, snapshot, ctx);
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
  renderQuestions();
  renderFeed();
  renderCallouts();
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
