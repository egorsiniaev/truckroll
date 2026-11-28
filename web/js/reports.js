// Reports: rules decide what applies, the AI drafts, a person approves. Nothing is ever sent.
import { esc, post, toast } from "./panel.js";

const NB = " ";
const list = document.getElementById("incident-list");
const dailyList = document.getElementById("daily-list");
const filingList = document.getElementById("filing-list");
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

const kv = (rows) => `<table class="kv">${rows.filter(Boolean).map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("")}</table>`;
const hrs = (min) => (min >= 90 ? `${(min / 60).toFixed(1)}${NB}h` : `${Math.round(min)}${NB}min`);

function due(iso) {
  if (!iso) return "–";
  const ms = new Date(iso).getTime() - Date.now();
  const h = Math.abs(ms) / 3.6e6;
  const rel = h >= 48 ? `${Math.round(h / 24)}${NB}days` : `${Math.round(h)}${NB}h`;
  return `<span class="deadline ${ms < 0 ? "late" : ""}">${fmt(iso)}</span><small>${ms < 0 ? `${rel} overdue` : `in ${rel}`}</small>`;
}

function verdictCard(name, v, line) {
  return `<div class="dr-card ${v === "reportable" ? "applies" : ""}"><span class="dr-name">${name}</span>
    ${verdict(v)}<p>${line}</p></div>`;
}

function dueTable(n, c) {
  const rows = [
    ...n.significant.map((x) => ({ reg: "NIS2", what: `${esc(x.title)} <small>${esc(x.id)}</small>`, why: esc(x.why), d: x.deadlines, pack: `nis2-${x.id}` })),
    ...c.exploited.map((x) => ({ reg: "CRA", what: `${esc(x.advisory)} fixed in&nbsp;${esc(x.version)}`, why: `actively exploited, ${esc(x.severity)} severity`, d: x.deadlines, pack: `cra-${x.advisory}` })),
  ];
  if (!rows.length) return `<p class="muted">Nothing to&nbsp;notify today.</p>`;
  return `<table class="due"><tr><th>Regime</th><th>What</th><th>Early warning</th><th>Notification</th><th>Final report</th></tr>
    ${rows.map((r) => `<tr><td><span class="badge nis2">${r.reg}</span></td><td><b>${r.what}</b><small>${r.why} · <a href="#" class="no-print" data-pack="${esc(r.pack)}">open filing pack</a></small></td>
      <td>${due(r.d.early_warning)}</td><td>${due(r.d.notification)}</td><td>${due(r.d.final_report)}</td></tr>`).join("")}</table>`;
}

function nis2Html(n) {
  const a = n.area_outage, f = n.financial, p = n.patching;
  return `<h4>NIS2 details</h4>
    <h5>Area outage</h5>
    ${a ? `<p>${esc(a.site)} was without power and&nbsp;line from ${time(a.started)} to&nbsp;${a.ended ? time(a.ended) : "now"} (${Math.round(a.minutes)}${NB}min).
      Sales lost: about <b>${eur(a.loss_eur)}</b> from drivers turned away, giving up or&nbsp;cut&nbsp;off.
      ${a.incident_id ? `Incident <a href="?incident=${encodeURIComponent(a.incident_id)}">${esc(a.incident_id)}</a>.` : ""} ${a.note ? `<span class="muted">${esc(a.note)}</span>` : ""}</p>`
      : `<p class="muted">No area outage on&nbsp;this&nbsp;day.</p>`}
    <h5>Impact today</h5>
    ${kv([["Incidents", `${n.incidents} (${n.significant.length} significant)`], ["Uptime", pct(n.uptime)], ["Revenue", eur(f.revenue_eur)],
      ["Sales lost to&nbsp;incidents and&nbsp;queues", eur(f.lost_revenue_eur)], ["Technician visits", eur(f.field_service_eur)]])}
    <h5>Patch management (Art.&nbsp;21: vulnerability handling)</h5>
    ${kv([["Releases", p.releases], ["Installs", `${p.installs_ok} ok, ${p.installs_failed} failed (${pct(p.success_rate)} success)`],
      ["Rolled back", p.rollbacks], ["Charger time out for&nbsp;updates", hrs(p.maintenance_min)],
      p.halted.length && ["Halted releases", `<b class="bad">${p.halted.map(esc).join(", ")}</b> (${p.reverted} chargers rolled back)`]])}`;
}

function craHtml(c) {
  const fl = c.fleet;
  return `<h4>CRA details</h4>
    ${kv([["On the&nbsp;latest version", `${fl.on_latest} of&nbsp;${fl.stations} chargers (${esc(fl.latest ?? "–")})`],
      ["Missing a&nbsp;security fix", fl.exposed ? `<b class="bad">${fl.exposed} chargers</b> (newest fix in&nbsp;${esc(fl.newest_security_fix)})` : "none"],
      ["Failed updates", c.updates.installs_failed], ["Rolled back", c.updates.rollbacks]])}
    ${c.security_fixes.length ? `<h5>Security fixes</h5><table class="due"><tr><th>Advisory</th><th>Version</th><th>Severity</th><th>Chargers patched</th><th>Median time to&nbsp;patch</th></tr>
      ${c.security_fixes.map((x) => `<tr><td>${esc(x.advisory)}${x.exploited ? ' <span class="badge nis2">exploited</span>' : ""}</td><td>${esc(x.version)}${x.halted ? ' <b class="bad">halted</b>' : ""}</td><td>${esc(x.severity)}</td><td>${x.halted ? "0" : x.patched}</td><td>${x.median_min == null ? "–" : `${Math.round(x.median_min)}${NB}min`}</td></tr>`).join("")}</table>` : ""}
    <h5>Releases</h5>
    <table class="due"><tr><th>Time</th><th>Version</th><th>Type</th><th>Notes</th></tr>
      ${c.releases.map((r) => `<tr><td>${time(r.time)}</td><td><b>${esc(r.version)}</b></td><td>${esc(r.bump)}</td><td>${esc(r.notes)}${r.security ? ` <span class="fw-sec">${esc(r.advisory)}</span>` : ""}</td></tr>`).join("") || '<tr><td colspan="4" class="muted">None.</td></tr>'}</table>`;
}

async function openDaily(day) {
  current = `day:${day}`;
  renderList();
  loadDaily();
  const rep = await (await fetch(`/api/daily/${encodeURIComponent(day)}`)).json();
  document.title = `NIS2 and CRA report ${day}`;
  detail.innerHTML = `<div class="dr-head"><h3>Daily NIS2 and&nbsp;CRA report, ${esc(day)}</h3>
      <button class="mini no-print" id="pdf">Download PDF</button></div>
    <p class="meta">${rep.preliminary ? "So far today; the final version is&nbsp;written at&nbsp;23:55." : `Written ${fmt(rep.generated)}.`}
      Built from rules and&nbsp;our own records; no&nbsp;AI. Truckroll never sends it.</p>
    <div class="dr-verdicts">
      ${verdictCard("NIS2 (Cyberbeveiligingswet)", rep.nis2.verdict, `${rep.nis2.significant.length} significant of&nbsp;${rep.nis2.incidents} incidents`)}
      ${verdictCard("CRA (Cyber Resilience Act)", rep.cra.verdict, esc(rep.cra.why))}
    </div>
    <h4>Due to&nbsp;the&nbsp;authority</h4>
    ${dueTable(rep.nis2, rep.cra)}
    ${nis2Html(rep.nis2)}${craHtml(rep.cra)}`;
  document.getElementById("pdf").onclick = () => window.print();
  detail.querySelectorAll("[data-pack]").forEach((a) => { a.onclick = (e) => { e.preventDefault(); openPack(a.dataset.pack); }; });
}

// ---- filing packs: what NIS2 and the CRA ask for, per reportable event -------------------------
const STATUS = { drafted: ["draft", "warn"], approved: ["approved, not sent", "warn"], sent: ["sent", "ok"] };
let packs = [];

function nextDue(p) {
  const s = p.stages.find((x) => x.status !== "sent" && x.deadline);
  return s ? `${esc(s.title.replace(/ \(.*\)/, ""))} ${due(s.deadline)}` : p.done ? '<span class="rp-ok">all stages sent</span>' : "";
}

async function loadFilings() {
  packs = (await (await fetch("/api/filings")).json()).filings;
  filingList.innerHTML = packs.map((p) => `<li data-pack="${esc(p.id)}" class="dl-row fl-row ${current === `pack:${p.id}` ? "active" : ""}">
    <span class="badge nis2">${p.regime.toUpperCase()}</span><b>${esc(p.title)}</b><span class="fl-next">${nextDue(p)}</span></li>`).join("")
    || `<li class="muted">Nothing to&nbsp;file.</li>`;
  filingList.querySelectorAll("[data-pack]").forEach((li) => { li.onclick = () => openPack(li.dataset.pack); });
}

function stageHtml(p, s) {
  const [label, cls] = STATUS[s.status] ?? [s.status, "warn"];
  const who = s.status === "sent" ? `Marked sent ${fmt(s.sent_at)}${s.reference ? `, reference ${esc(s.reference)}` : ""}.`
    : s.status === "approved" ? `Approved ${fmt(s.approved_at)}.` : "";
  const actions = s.status === "drafted" ? `<button class="mini primary" data-act="approve" data-stage="${s.key}">Approve</button>`
    : s.status === "approved" ? `<input class="fl-ref" data-ref="${s.key}" placeholder="Portal reference (optional)" maxlength="80">
        <button class="mini primary" data-act="sent" data-stage="${s.key}">Mark as&nbsp;sent</button>` : "";
  return `<section class="fl-stage">
    <header><h4>${esc(s.title)}</h4><span class="rp-pill ${cls}">${label}</span>
      ${s.deadline ? `<span class="fl-due">due ${due(s.deadline)}</span>` : ""}</header>
    <table class="kv fl-fields">${s.fields.map((f, i) => `<tr><td>${esc(f.label)}</td><td><div class="fl-cell"><span class="fl-val">${esc(f.value)}</span>
      <button class="copy no-print" data-copy="${s.key}:${i}" title="Copy">Copy</button></div></td></tr>`).join("")}</table>
    <p class="meta no-print">${who} ${actions}</p></section>`;
}

async function openPack(id) {
  current = `pack:${id}`;
  renderList(); loadDaily(); loadFilings();
  const p = await (await fetch(`/api/filings/${encodeURIComponent(id)}`)).json();
  document.title = `${p.regime.toUpperCase()} ${p.reference} filing`;
  detail.innerHTML = `<div class="dr-head"><h3>${p.regime.toUpperCase()} filing: ${esc(p.title)}</h3>
      <button class="mini no-print" id="pdf">Download PDF</button></div>
    <p class="meta">${esc(p.reference)} · reportable because ${esc(p.why)} · file through ${esc(p.channel)}.
      Prepared from our records; a&nbsp;person approves each stage, files it in&nbsp;the&nbsp;portal and&nbsp;marks it sent. Truckroll never sends anything.</p>
    ${p.stages.map((s) => stageHtml(p, s)).join("")}
    <p class="meta print-only">Printed ${fmt(new Date().toISOString())} from Truckroll (simulated operator). Statuses as recorded at that moment.</p>`;
  document.getElementById("pdf").onclick = () => window.print();
  detail.querySelectorAll("[data-copy]").forEach((b) => {
    b.onclick = async () => {
      const [stage, i] = b.dataset.copy.split(":");
      await navigator.clipboard.writeText(p.stages.find((s) => s.key === stage).fields[Number(i)].value);
      toast("Copied");
    };
  });
  detail.querySelectorAll("[data-act]").forEach((b) => {
    b.onclick = async () => {
      const ref = detail.querySelector(`[data-ref="${b.dataset.stage}"]`)?.value ?? "";
      if (await post(`/api/filings/${encodeURIComponent(id)}/${b.dataset.stage}/${b.dataset.act}`, { reference: ref }, b)) openPack(id);
    };
  });
}

only.onchange = renderList;
loadFilings().then(() => {
  const first = new URLSearchParams(location.search).get("filing");
  if (first) openPack(first);
}).catch((e) => toast(`Failed: ${e.message}`, true));
loadDaily().catch((e) => toast(`Failed: ${e.message}`, true));
loadList().then(() => {
  const first = new URLSearchParams(location.search).get("incident");
  if (first) open(first);
}).catch((e) => toast(`Failed: ${e.message}`, true));
