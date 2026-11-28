"""Filing packs: the documents NIS2 and the CRA actually ask for, one pack per reportable event.

NIS2 (Cyberbeveiligingswet): a significant incident gets an early warning (24 h), an incident notification (72 h) and
a final report (one month after the notification), filed once through the NCSC portal, which forwards it to the
supervisor. CRA: an actively exploited vulnerability in our firmware gets an early warning (24 h), a notification
(72 h) and a final report (14 days after the fix is out) through ENISA's Single Reporting Platform, plus an advisory
for users. Fields are filled from our own records; a person approves each stage and marks it sent. Nothing is sent
from here.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from control.reports import applicability

log = logging.getLogger(__name__)

ENTITY = "Truckroll (simulated): charging point operator, sector energy (electricity), the Netherlands"
PRODUCT = "Truckroll charger firmware (product with digital elements)"
NIS2_CHANNEL = "NCSC portal mijn.ncsc.nl (forwarded to the supervisor, RDI)"
CRA_CHANNEL = "ENISA Single Reporting Platform (routed to the coordinating CSIRT, NL)"
STAGES = {
    "nis2": [("early_warning", "Early warning (24 h)"), ("notification", "Incident notification (72 h)"),
             ("final_report", "Final report (1 month after the notification)")],
    "cra": [("early_warning", "Early warning (24 h)"), ("notification", "Vulnerability notification (72 h)"),
            ("final_report", "Final report (14 days after the fix)"), ("user_advisory", "Advisory to users")],
}
CAUSE = {
    "site_outage": "The site lost its connection to our backend (power or line loss at the site). No sign of an "
                   "attack: no unexpected logins, configuration changes or traffic before or during the outage.",
    "auth_slow": "The card authorisation service answered too slowly, so sessions failed to start. Capacity problem "
                 "in our backend, no sign of an attack.",
    "backlog": "Our charging backend (CSMS) was overloaded and replied late to chargers. No sign of an attack.",
}
NO_MALICE = "Not suspected. Nothing so far points to unlawful or malicious acts."
NO_CROSS_BORDER = "No. All affected sites and customers are in the Netherlands; no service is provided in other Member States."


def _fmt(iso: str | None) -> str:
    if not iso:
        return "-"
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(UTC).strftime("%d %b %Y %H:%M UTC")


def _field(label: str, value) -> dict:
    return {"label": label, "value": str(value)}


def _responses(inc: Mapping) -> str:
    lines = [f"{_fmt(t['time'])}: {t['text']}" for t in inc.get("timeline", []) if t["type"] in ("response", "recovered")]
    return "\n".join(lines) or "No response recorded yet."


def _scope(inc: Mapping, sites: Mapping[str, Mapping]) -> str:
    site = sites.get(inc["site"])
    if inc["site"] == "ALL":
        n = sum(s.get("stations", 0) for s in sites.values())
        return f"All {len(sites)} sites, {n} chargers"
    if inc.get("stations"):
        return f"{inc['site']}: {len(inc['stations'])} charger(s) ({', '.join(inc['stations'][:6])})"
    return f"{inc['site']}: the whole site, {site.get('stations', '?')} chargers" if site else inc["site"]


def nis2_pack(inc: Mapping, sites: Mapping[str, Mapping], loss_eur: float | None) -> dict:
    a = applicability(inc)["nis2"]
    m = inc.get("metrics") or {}
    duration = ((inc.get("ttr_s") or inc.get("age_s") or 0) + (m.get("ttd_s") or 0)) // 60
    recovered = inc["status"] == "recovered"
    detected = next((t["text"] for t in inc.get("timeline", []) if t["type"] == "detected"), inc["title"])
    loss = f"About €{loss_eur:.0f} in sales lost (drivers turned away, gave up or cut off)." if loss_eur is not None \
        else "Not measured for this incident."
    base = [_field("Entity", ENTITY), _field("Incident reference", inc["id"]),
            _field("Became aware", _fmt(inc["opened"])), _field("Affected service", "Public EV charging"),
            _field("Affected", _scope(inc, sites))]
    stages = {
        "early_warning": base + [
            _field("What happened", f"{inc['title']}. {detected}"),
            _field("Suspected unlawful or malicious act", NO_MALICE),
            _field("Possible cross-border impact", NO_CROSS_BORDER)],
        "notification": base + [
            _field("Update to the early warning", f"{inc['title']}. Status: {'resolved' if recovered else inc['status']}."),
            _field("Initial assessment: severity", f"{inc['severity']}; {a['why']}."),
            _field("Initial assessment: impact", f"Service unavailable for about {duration} min. {loss}"),
            _field("Indicators of compromise", "None identified."),
            _field("Measures taken so far", _responses(inc))],
        "final_report": base + [
            _field("Detailed description", f"{inc['title']}. {detected} Duration about {duration} min. {loss}"),
            _field("Severity and impact", f"{inc['severity']}; {a['why']}."),
            _field("Type of threat or root cause", CAUSE.get(inc["kind"], "See the incident timeline; root cause "
                                                                        "analysis by the operator.")),
            _field("Mitigation measures applied and ongoing", _responses(inc)),
            _field("Cross-border impact", NO_CROSS_BORDER)]
        + ([] if recovered else [_field("Note", "Incident still ongoing at the deadline: file a progress report "
                                                "instead, and the final report within one month of handling it.")]),
    }
    return {"id": f"nis2-{inc['id']}", "regime": "nis2", "title": inc["title"], "reference": inc["id"],
            "channel": NIS2_CHANNEL, "why": a["why"], "deadlines": a["deadlines"], "opened": inc["opened"],
            "stages": stages}


def cra_pack(release: Mapping, versions: Mapping[str, str], patched_on: set[str]) -> dict:
    v, adv = release["version"], release["advisory"]
    total = len(versions)
    patched = len(patched_on)
    rollout = f"{patched} of {total} chargers run {v} or newer."
    from control.compliance import _cra_deadlines  # noqa: PLC0415 - shared deadline rule
    released = datetime.fromisoformat(release["time"])
    base = [_field("Manufacturer", ENTITY), _field("Product", PRODUCT), _field("Advisory", adv),
            _field("Severity", release["security"]), _field("Became aware of active exploitation", _fmt(release["time"]))]
    stages = {
        "early_warning": base + [
            _field("Affected versions", f"All versions before {v}"),
            _field("Member States where the product is available", "The Netherlands"),
            _field("Corrective measure", f"Available: firmware {v}.")],
        "notification": base + [
            _field("General nature of the vulnerability", f"{release['security'].capitalize()} severity flaw in the "
                                                          f"charger firmware, fixed in {v} ({release['notes']})."),
            _field("General nature of the exploit", "Actively exploited; exploit details are being analysed."),
            _field("Corrective or mitigating measures", f"Update to firmware {v}. Our fleet updates free chargers "
                                                        f"automatically. {rollout}"),
            _field("Users can take", "No action needed for chargers we operate; other owners should install the "
                                     "update.")],
        "final_report": base + [
            _field("Description, severity and impact", f"{release['security'].capitalize()} severity vulnerability "
                                                        f"in the charger firmware, actively exploited, fixed in {v}."),
            _field("Malicious actor", "Not identified."),
            _field("Security update", f"Firmware {v}, released {_fmt(release['time'])}. {rollout}"),
            _field("Other corrective measures", "Monitoring for repeat exploitation; vulnerability added to the "
                                                "regression tests.")],
        "user_advisory": [
            _field("Title", f"Security update {v} fixes an actively exploited vulnerability ({adv})"),
            _field("Text", f"A {release['security']} severity vulnerability in our charger firmware is being "
                           f"exploited. Firmware {v} fixes it. Chargers we operate update automatically while "
                           f"free; {rollout} If you run Truckroll chargers yourself, install {v} now.")],
    }
    return {"id": f"cra-{adv}", "regime": "cra", "title": f"{adv} fixed in {v}", "reference": adv,
            "channel": CRA_CHANNEL, "why": f"actively exploited vulnerability, {release['security']} severity",
            "deadlines": {**_cra_deadlines(released, 14), "user_advisory": None}, "opened": release["time"],
            "stages": stages}


class Filings:
    """Status per pack and stage: drafted -> approved -> sent. Stored next to the reports."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.state: dict = {}
        if path and path.exists():
            try:
                self.state = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("filings state unreadable, starting fresh: %s", exc)

    def _save(self) -> None:
        if self.path:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=1))
            tmp.replace(self.path)

    def status(self, pack_id: str, stage: str) -> dict:
        return self.state.get(pack_id, {}).get(stage, {"status": "drafted"})

    def mark(self, pack: Mapping, stage: str, status: str, reference: str = "", by: str = "operator") -> dict:
        if stage not in {s for s, _ in STAGES[pack["regime"]]}:
            raise KeyError(stage)
        if status == "sent" and self.status(pack["id"], stage)["status"] != "approved":
            raise ValueError("approve the draft before marking it sent")
        entry = {**self.status(pack["id"], stage), "status": status,
                 f"{status}_at": datetime.now(UTC).isoformat(timespec="seconds"), f"{status}_by": by}
        if reference:
            entry["reference"] = reference[:80]
        self.state = {**self.state, pack["id"]: {**self.state.get(pack["id"], {}), stage: entry}}
        self._save()
        return entry

    def decorate(self, pack: dict) -> dict:
        stages = [{"key": k, "title": t, "deadline": pack["deadlines"].get(k), "fields": pack["stages"][k],
                   **self.status(pack["id"], k)} for k, t in STAGES[pack["regime"]]]
        open_ = [s for s in stages if s["status"] != "sent"]
        return {**{k: v for k, v in pack.items() if k != "stages"}, "stages": stages,
                "next": open_[0]["key"] if open_ else None, "done": not open_}


def due_soon(packs: list[dict], now: datetime, within_h: float = 6) -> list[dict]:
    """Stages not yet sent whose deadline is within the window or already passed, soonest first."""
    out = []
    for p in packs:
        for s in p["stages"]:
            if s["status"] == "sent" or not s.get("deadline"):
                continue
            left_h = (datetime.fromisoformat(s["deadline"]) - now).total_seconds() / 3600
            if left_h <= within_h:
                out.append({"id": p["id"], "regime": p["regime"].upper(), "title": p["title"], "stage": s["title"],
                            "deadline": s["deadline"], "left_h": round(left_h, 1)})
                break  # one reminder per pack: its next stage
    return sorted(out, key=lambda x: x["left_h"])
