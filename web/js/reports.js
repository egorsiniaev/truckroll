// Reports: rules decide what applies, the AI drafts, a person approves. Nothing is ever sent.
import { esc, post, toast } from "./panel.js";

const NB = " ";
const list = document.getElementById("incident-list");
const detail = document.getElementById("detail");
const only = document.getElementById("only-reportable");
let rows = [];
let current = null;

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

function renderList() {
  const shown = only.checked ? rows.filter((r) => r.applicability.nis2.applies || r.applicability.cra.applies) : rows;
  list.innerHTML = shown.map((r) => {
    const reg = r.applicability.nis2.applies ? '<span class="badge nis2">NIS2</span>' : r.applicability.cra.applies ? '<span class="badge nis2">CRA</span>' : '<span class="badge none">not reportable</span>';
    const rep = r.has_report ? (r.approved.postmortem ? '<span class="badge ok">approved</span>' : '<span class="badge draft">draft</span>') : "";
    return `<li data-id="${esc(r.id)}" class="${current === r.id ? "active" : ""}"><b>${esc(r.title)}</b>${reg}${rep}
      <span class="muted">${esc(r.id)} · ${fmt(r.opened)} · ${r.status === "recovered" ? `resolved in ${dur(r.ttr_s)}` : r.status}</span></li>`;
  }).join("") || `<li class="muted">Nothing here.</li>`;
  list.querySelectorAll("[data-id]").forEach((li) => { li.onclick = () => open(li.dataset.id); });
}

function regime(name, a) {
  const d = a.deadlines || {};
  return `<div class="regime ${a.applies ? "applies" : ""}"><b>${name}: ${a.applies ? "reportable" : "not reportable"}</b> <span class="muted">${esc(a.why)}</span>
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
    <p class="meta">${esc(i.id)} · ${esc(i.site)} · opened ${fmt(i.opened)} · ${i.status === "recovered" ? `resolved ${fmt(i.recovered)}` : esc(i.status)}</p>
    <p class="meta">Detect ${dur(m.ttd_s)} · acknowledge ${dur(m.tta_s)} · mitigate ${dur(m.ttm_s)} · resolve ${dur(m.ttr_s)}${m.field_visit ? " · field visit" : ""}</p>
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
    <h4>Timeline</h4><ul class="rules">${i.timeline.map((t) => `<li><span class="muted">${fmt(t.time)}</span> <b>${esc(t.type)}</b> ${esc(t.text)} <span class="muted">${esc(t.actor)}</span></li>`).join("")}</ul>`;
  document.getElementById("draft").onclick = async (e) => {
    e.target.textContent = "Drafting…";
    const res = await post(`/api/reports/${encodeURIComponent(id)}/draft`, {}, e.target);
    if (res) { await loadList(); open(id); }
  };
  detail.querySelectorAll("[data-approve]").forEach((b) => {
    b.onclick = async () => { if (await post(`/api/reports/${encodeURIComponent(id)}/approve/${b.dataset.approve}`, {}, b)) { await loadList(); open(id); } };
  });
}

only.onchange = renderList;
loadList().then(() => {
  const first = new URLSearchParams(location.search).get("incident");
  if (first) open(first);
}).catch((e) => toast(`Failed: ${e.message}`, true));
