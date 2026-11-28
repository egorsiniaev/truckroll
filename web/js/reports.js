// Reports: rules decide what applies, the AI drafts, a person approves. Nothing is ever sent.
import { esc, post, toast } from "./panel.js";

const NB = " ";
const list = document.getElementById("incident-list");
const dailyList = document.getElementById("daily-list");
const detail = document.getElementById("detail");
const only = document.getElementById("only-reportable");
let rows = [];
let current = null;

// Same step colours and actor names as the incidents list on the main page.
const ACTOR = { system: "system", "auto-rule": "auto rule", operator: "operator", scenario: "scenario", agent: "AI agent", technician: "technician" };
const time = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "–");
const metric = (label, v, missing = "–") => `<span class="rp-metric"><span>${label}</span><b>${v == null ? missing : dur(v)}</b></span>`;
const fmt = (iso) => (iso ? new Date(iso).toLocaleString([], { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "–");
const dur = (s) => (s == null ? "–" : s >= 3600 ? `${(s / 3600).toFixed(1)}${NB}h` : s >= 60 ? `${Math.round(s / 60)}${NB}min` : `${s}${NB}s`);

function left(iso) {
  const ms = new Date(iso).getTime() - Date.now();
  const late = ms < 0;
  const h = Math.abs(ms) / 3.6e6;
  const text = h >= 48 ? `${Math.round(h / 24)}${NB}days` : `${h.toFixed(h < 10 ? 1 : 0)}${NB}h`;
  return `<span class="deadline ${late ? "late" : ""}">${fmt(iso)} (${late ? `${text} overdue` : `in ${text}`})</span>`;
}

async function loadList() {
  rows = (await (await fetch("/api/reports")).json()).incidents;
  renderList();
}

function dayLabel(iso) {
  const d = new Date(iso), today = new Date();
  const key = (x) => x.toDateString();
  const yesterday = new Date(today); yesterday.setDate(today.getDate() - 1);
  return key(d) === key(today) ? "Today" : key(d) === key(yesterday) ? "Yesterday"
    : d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
}

function renderList() {
  const shown = only.checked ? rows.filter((r) => r.applicability.nis2.applies || r.applicability.cra.applies) : rows;
  let day = "";
  list.innerHTML = shown.map((r) => {
    const label = dayLabel(r.opened);
    const head = label !== day ? `<li class="rp-day">${esc((day = label))}</li>` : "";
    const regs = [r.applicability.nis2.applies && "NIS2", r.applicability.cra.applies && "CRA"].filter(Boolean)
      .map((x) => `<span class="badge nis2">${x}</span>`).join("");
    const rep = r.has_report ? (r.approved.postmortem ? '<span class="badge ok">approved</span>' : '<span class="badge draft">draft</span>') : "";
    const state = r.status === "recovered" ? `<span class="rp-ok">resolved in ${dur(r.ttr_s)}</span>`
      : `<span class="rp-open">${esc(r.status)}</span>`;
    const at = new Date(r.opened).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return `${head}<li data-id="${esc(r.id)}" class="rp-row ${esc(r.status)} ${current === r.id ? "active" : ""}">
      <i class="sev"></i><b class="rp-title">${esc(r.title)}</b><time>${at}</time>
      <span class="rp-meta">${regs}${rep}<span>${esc(r.id)}</span><span class="rp-tag">${esc(r.site)}</span>
        <span class="rp-tag">${esc(r.kind.replaceAll("_", " "))}</span>${state}</span></li>`;
  }).join("") || `<li class="muted">Nothing here.</li>`;
  list.querySelectorAll("[data-id]").forEach((li) => { li.onclick = () => open(li.dataset.id); });
}

function regime(name, a) {
  const d = a.deadlines || {};
  return `<div class="regime ${a.applies ? "applies" : ""}"><b>${name}</b>
    <span class="rp-pill ${a.applies ? "bad" : "ok"}">${a.applies ? "reportable" : "not reportable"}</span> <span class="muted">${esc(a.why)}</span>
    ${a.applies ? `<br>Early warning ${left(d.early_warning)}<br>Notification ${left(d.notification)}<br>Final report ${left(d.final_report)}` : ""}</div>`;
}

function part(title, key, text, rep) {
  if (!text) return "";
  const ok = rep.approved?.[key];
  return `<h4>${title}</h4><div class="draft">${esc(text)}</div>
    ${ok ? `<p class="approved">Approved by ${esc(ok.by)}, ${fmt(ok.at)}. Send it yourself; Truckroll doesn’t submit.</p>` : `<button class="mini primary" data-approve="${key}">Approve draft</button>`}`;
}

async function open(id) {
  current = id;
  renderList();
  const { incident: i, applicability: a, report: rep } = await (await fetch(`/api/reports/${encodeURIComponent(id)}`)).json();
  const m = i.metrics || {};
  const d = rep?.doc;
  detail.innerHTML = `
    <h3>${esc(i.title)}</h3>
    <p class="meta">${esc(i.id)} · ${esc(i.site)} · opened ${fmt(i.opened)}
      <span class="rp-pill ${i.status === "recovered" ? "ok" : i.status === "monitoring" ? "warn" : "bad"}">${i.status === "recovered" ? `resolved ${fmt(i.recovered)}` : esc(i.status)}</span></p>
    <div class="rp-metrics">${metric("detect", m.ttd_s)}${metric("acknowledge", m.tta_s, "nobody (defaults)")}${metric("mitigate", m.ttm_s)}${metric("resolve", m.ttr_s, i.status === "recovered" ? "–" : "still open")}${m.field_visit ? '<span class="rp-metric"><span>field visit</span><b>yes</b></span>' : ""}</div>
    ${regime("NIS2 (Cyberbeveiligingswet)", a.nis2)}${regime("CRA", a.cra)}
    <div class="control"><button class="mini ${d ? "" : "primary"}" id="draft">${d ? "Redraft with AI" : "Draft with AI"}</button>
      <span class="muted">One Gemini call. You review and&nbsp;approve every part.</span></div>
    ${d ? `<div class="doc">
      <h4>Post-mortem</h4>
      <p><b>Summary.</b> ${esc(d.summary)}</p><p><b>Impact.</b> ${esc(d.impact)}</p><p><b>Root cause.</b> ${esc(d.root_cause)}</p>
      <p><b>What worked.</b> ${esc(d.what_worked)}</p><p><b>What to improve.</b> ${esc(d.what_to_improve)}</p>
      <p><b>Actions.</b></p><ul class="rules">${(d.actions || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
      ${rep.approved?.postmortem ? `<p class="approved">Post-mortem approved by ${esc(rep.approved.postmortem.by)}, ${fmt(rep.approved.postmortem.at)}.</p>` : `<button class="mini primary" data-approve="postmortem">Approve post-mortem</button>`}
      ${a.nis2.applies ? part("NIS2 early warning (24 h)", "nis2_early_warning", d.nis2_early_warning, rep) + part("NIS2 incident notification (72 h)", "nis2_notification", d.nis2_notification, rep) : ""}
      ${a.cra.applies ? part("CRA early warning (24 h)", "cra_early_warning", d.cra_early_warning, rep) : ""}
      <p class="meta">Drafted ${fmt(rep.generated)} by ${esc(rep.model)} (${rep.tokens?.[0]} tokens in, ${rep.tokens?.[1]} out).</p></div>`
      : `<p class="hint">No draft yet.</p>`}
    <h4>Timeline</h4><ol class="rp-timeline">${i.timeline.map((t) => `<li class="${esc(t.type)}">
      <time>${time(t.time)}</time><span class="step ${esc(t.type)}">${esc(t.short || t.type)}</span>
      <span class="rp-actor ${esc(t.actor)}">${esc(ACTOR[t.actor] ?? t.actor)}</span><p>${esc(t.text)}</p></li>`).join("")}</ol>`;
  document.getElementById("draft").onclick = async (e) => {
    e.target.textContent = "Drafting…";
    const res = await post(`/api/reports/${encodeURIComponent(id)}/draft`, {}, e.target);
    if (res) { await loadList(); open(id); }
  };
  detail.querySelectorAll("[data-approve]").forEach((b) => {
    b.onclick = async () => { if (await post(`/api/reports/${encodeURIComponent(id)}/approve/${b.dataset.approve}`, {}, b)) { await loadList(); open(id); } };
  });
}

// ---- daily NIS2 / CRA report -------------------------------------------------------------
const eur = (v) => (v == null ? "–" : `€${Math.round(v).toLocaleString()}`);
const pct = (v) => (v == null ? "–" : `${(v * 100).toFixed(1)}${NB}%`);
const verdict = (v) => `<span class="rp-pill ${v === "reportable" ? "bad" : "ok"}">${esc(v)}</span>`;

async function loadDaily() {
  const { days } = await (await fetch("/api/daily")).json();
  dailyList.innerHTML = days.map((d) => `<li data-day="${esc(d.day)}" class="dl-row ${current === `day:${d.day}` ? "active" : ""}">
    <b>${esc(dayLabel(`${d.day}T12:00:00`))}${d.preliminary ? " (so&nbsp;far)" : ""}</b>
    <span>${d.nis2 === "reportable" ? '<span class="badge nis2">NIS2</span>' : ""}${d.cra === "reportable" ? '<span class="badge nis2">CRA</span>' : ""}${d.nis2 !== "reportable" && d.cra !== "reportable" ? '<span class="badge ok">nothing to report</span>' : ""}</span>
    <span class="muted">${esc(d.day)}</span></li>`).join("") || `<li class="muted">No daily reports yet.</li>`;
  dailyList.querySelectorAll("[data-day]").forEach((li) => { li.onclick = () => openDaily(li.dataset.day); });
}

function deadlines(d) {
  return d && d.early_warning ? `<br>Early warning ${left(d.early_warning)}<br>Notification ${left(d.notification)}<br>Final report ${left(d.final_report)}` : "";
}

function nis2Html(n) {
  const a = n.area_outage, f = n.financial, p = n.patching;
  return `<div class="regime ${n.verdict === "reportable" ? "applies" : ""}"><b>NIS2 (Cyberbeveiligingswet)</b> ${verdict(n.verdict)}
      <span class="muted">${n.incidents} incidents, ${n.significant.length} significant</span></div>
    ${n.significant.map((x) => `<div class="regime applies"><b>${esc(x.title)}</b> <span class="muted">${esc(x.id)} · ${esc(x.why)}</span>${deadlines(x.deadlines)}</div>`).join("")}
    <h4>Area outage</h4>
    ${a ? `<p>${esc(a.site)} without power and&nbsp;line from ${time(a.started)} to&nbsp;${a.ended ? time(a.ended) : "now"} (${Math.round(a.minutes)}${NB}min).
      Lost revenue about <b>${eur(a.loss_eur)}</b> from drivers turned away, giving up or&nbsp;cut&nbsp;off.
      ${a.incident_id ? `Incident <a href="?incident=${encodeURIComponent(a.incident_id)}">${esc(a.incident_id)}</a>.` : ""} ${a.note ? `<span class="muted">${esc(a.note)}</span>` : ""}</p>`
      : `<p class="muted">No area outage on this&nbsp;day.</p>`}
    <h4>Money and&nbsp;service</h4>
    <div class="rp-metrics"><span class="rp-metric"><span>lost revenue (day)</span><b>${eur(f.lost_revenue_eur)}</b></span>
      <span class="rp-metric"><span>technician visits</span><b>${eur(f.field_service_eur)}</b></span>
      <span class="rp-metric"><span>revenue</span><b>${eur(f.revenue_eur)}</b></span>
      <span class="rp-metric"><span>uptime</span><b>${pct(n.uptime)}</b></span></div>
    <h4>Patch management (Art.&nbsp;21: vulnerability handling)</h4>
    <div class="rp-metrics"><span class="rp-metric"><span>releases</span><b>${p.releases}</b></span>
      <span class="rp-metric"><span>installs ok / failed</span><b>${p.installs_ok} / ${p.installs_failed}</b></span>
      <span class="rp-metric"><span>success rate</span><b>${pct(p.success_rate)}</b></span>
      <span class="rp-metric"><span>rollbacks</span><b>${p.rollbacks}</b></span>
      <span class="rp-metric"><span>chargers out for&nbsp;updates</span><b>${Math.round(p.maintenance_min)}${NB}min</b></span></div>
    ${p.halted.length ? `<p class="bad">Halted releases: ${p.halted.map(esc).join(", ")} (${p.reverted} chargers rolled back).</p>` : ""}`;
}

function craHtml(c) {
  const fl = c.fleet;
  return `<div class="regime ${c.verdict === "reportable" ? "applies" : ""}"><b>CRA (Cyber Resilience Act)</b> ${verdict(c.verdict)}
      <span class="muted">${esc(c.why)}</span></div>
    ${c.exploited.map((x) => `<div class="regime applies"><b>${esc(x.advisory)} in&nbsp;${esc(x.version)}</b> <span class="muted">actively exploited, ${esc(x.severity)}</span>${deadlines(x.deadlines)}</div>`).join("")}
    <div class="rp-metrics"><span class="rp-metric"><span>on latest (${esc(fl.latest ?? "–")})</span><b>${fl.on_latest} of&nbsp;${fl.stations}</b></span>
      <span class="rp-metric"><span>missing a&nbsp;security fix</span><b class="${fl.exposed ? "bad" : ""}">${fl.exposed}</b></span>
      <span class="rp-metric"><span>failed updates</span><b>${c.updates.installs_failed}</b></span>
      <span class="rp-metric"><span>rolled back</span><b>${c.updates.rollbacks}</b></span></div>
    ${c.security_fixes.length ? `<h4>Security fixes</h4><table><tr><th>Advisory</th><th>Version</th><th>Severity</th><th>Chargers patched</th><th>Median time to&nbsp;patch</th></tr>
      ${c.security_fixes.map((x) => `<tr><td>${esc(x.advisory)}${x.exploited ? ' <span class="badge nis2">exploited</span>' : ""}</td><td>${esc(x.version)}${x.halted ? ' <b class="bad">halted, rolled&nbsp;back</b>' : ""}</td><td>${esc(x.severity)}</td><td>${x.halted ? "0" : x.patched}</td><td>${x.median_min == null ? "–" : `${Math.round(x.median_min)}${NB}min`}</td></tr>`).join("")}</table>` : ""}
    <h4>Releases</h4>
    <ol class="rp-timeline">${c.releases.map((r) => `<li><time>${time(r.time)}</time><span class="step">${esc(r.version)}</span>
      <span class="rp-actor">${esc(r.bump)}</span><p>${esc(r.notes)}${r.security ? ` · security fix ${esc(r.advisory)} (${esc(r.security)})` : ""}</p></li>`).join("") || '<li class="muted">None.</li>'}</ol>`;
}

async function openDaily(day) {
  current = `day:${day}`;
  renderList();
  loadDaily();
  const rep = await (await fetch(`/api/daily/${encodeURIComponent(day)}`)).json();
  detail.innerHTML = `<h3>Daily NIS2 and&nbsp;CRA report, ${esc(day)}</h3>
    <p class="meta">${rep.preliminary ? "So far today; the final version is&nbsp;written at&nbsp;23:55." : `Written ${fmt(rep.generated)}.`}
      Built from rules and&nbsp;our own records; no&nbsp;AI. Truckroll never sends it.</p>
    <p><b>${esc(rep.headline)}</b></p>
    ${nis2Html(rep.nis2)}${craHtml(rep.cra)}`;
}

only.onchange = renderList;
loadDaily().catch((e) => toast(`Failed: ${e.message}`, true));
loadList().then(() => {
  const first = new URLSearchParams(location.search).get("incident");
  if (first) open(first);
}).catch((e) => toast(`Failed: ${e.message}`, true));
