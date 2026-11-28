"""Post-mortems and regulatory report drafts. Rules decide what applies; the AI drafts; a person approves.
Nothing here is ever sent to an authority.

NIS2 (NL: Cyberbeveiligingswet, in force 2026): operators of recharging points are entities in the energy sector.
A significant incident gets an early warning within 24 h of becoming aware, a notification within 72 h and a final
report within one month, to the CSIRT / competent authority.
CRA (reporting obligations since 11 Sep 2026): manufacturers report actively exploited vulnerabilities and severe
incidents affecting the security of their product: early warning 24 h, notification 72 h, final report 14 days after
a fix (vulnerability) or one month (incident), via the ENISA single reporting platform.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

SIGNIFICANT_SITE_OUTAGE_S = 1800
FLEET_WIDE = {"auth_slow", "backlog"}
SECURITY_KINDS: set[str] = set()  # none of today's scenarios is a security event; kept explicit on purpose


def _t(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def add_month(dt: datetime) -> datetime:
    """Same day next month, clamped to the month's last day (31 Jan -> 28/29 Feb)."""
    import calendar
    year, month = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    return dt.replace(year=year, month=month, day=min(dt.day, calendar.monthrange(year, month)[1]))


def nis2_deadlines(aware: datetime) -> dict:
    """NIS2 Art. 23(4): early warning 24 h and notification 72 h after awareness; final report one month after the
    notification (taken at its deadline)."""
    notification = aware + timedelta(hours=72)
    return {"early_warning": (aware + timedelta(hours=24)).isoformat(), "notification": notification.isoformat(),
            "final_report": add_month(notification).isoformat()}


def applicability(inc: dict) -> dict:
    """Which reporting regimes apply, and why. Deliberately conservative and explainable."""
    # From the first symptom, not the alert: a 30 min outage is 30 min even if detection took a few seconds.
    duration = (inc.get("ttr_s") or inc.get("age_s") or 0) + ((inc.get("metrics") or {}).get("ttd_s") or 0)
    reasons = []
    if inc["kind"] == "site_outage" and duration >= SIGNIFICANT_SITE_OUTAGE_S:
        reasons.append(f"a whole site was unavailable for {duration // 60} min")
    if inc["kind"] in FLEET_WIDE and duration >= 600:
        reasons.append("a backend failure affected every site")
    if inc["kind"] in SECURITY_KINDS:
        reasons.append("security-related")
    aware = _t(inc["opened"])
    nis2 = {
        "applies": bool(reasons),
        "why": "; ".join(reasons) or "no severe disruption or financial loss: not a significant incident",
        "deadlines": nis2_deadlines(aware) if reasons else {},
    }
    cra = {
        "applies": inc["kind"] in SECURITY_KINDS,
        "why": "no actively exploited vulnerability or security incident in a product we make"
        if inc["kind"] not in SECURITY_KINDS else "security incident affecting a product with digital elements",
        "deadlines": {},
    }
    return {"nis2": nis2, "cra": cra}


SYSTEM = (
    "You write incident documents for an EV charging network operator. Be brief, factual and blameless; use only "
    "the facts given; never invent numbers. Post-mortem: summary (2 sentences), impact (numbers from the facts), "
    "root cause, what worked, what to improve, up to 3 actions with an owner role. For each regulation marked as "
    "applicable, draft the early warning and the notification in the form the law asks for: NIS2 early warning "
    "says whether the incident is suspected to be caused by unlawful or malicious acts and whether it may have a "
    "cross-border impact; the notification adds an initial assessment of severity and impact and indicators of "
    "compromise if any. Leave drafts empty for regulations that do not apply."
)
SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary": {"type": "STRING"}, "impact": {"type": "STRING"}, "root_cause": {"type": "STRING"},
        "what_worked": {"type": "STRING"}, "what_to_improve": {"type": "STRING"},
        "actions": {"type": "ARRAY", "items": {"type": "STRING"}},
        "nis2_early_warning": {"type": "STRING"}, "nis2_notification": {"type": "STRING"},
        "cra_early_warning": {"type": "STRING"},
    },
    "required": ["summary", "impact", "root_cause", "what_worked", "what_to_improve", "actions"],
}


class Reports:
    def __init__(self, folder: Path | None) -> None:
        self.folder = folder
        if folder:
            folder.mkdir(parents=True, exist_ok=True)

    def _path(self, incident_id: str) -> Path | None:
        return self.folder / f"{incident_id}.json" if self.folder else None

    def get(self, incident_id: str) -> dict | None:
        p = self._path(incident_id)
        if p and p.exists():
            try:
                return json.loads(p.read_text())
            except json.JSONDecodeError:
                return None
        return None

    def save(self, report: dict) -> None:
        p = self._path(report["incident_id"])
        if p:
            p.write_text(json.dumps(report, indent=1))

    def _daily_path(self, day: str) -> Path | None:
        return self.folder / f"daily-{day}.json" if self.folder else None

    def save_daily(self, report: dict) -> None:
        p = self._daily_path(report["day"])
        if p:
            p.write_text(json.dumps(report, indent=1))

    def get_daily(self, day: str) -> dict | None:
        p = self._daily_path(day)
        if p and p.exists():
            try:
                return json.loads(p.read_text())
            except json.JSONDecodeError:
                return None
        return None

    def daily_days(self) -> list[str]:
        if not self.folder:
            return []
        return sorted((p.stem.removeprefix("daily-") for p in self.folder.glob("daily-*.json")), reverse=True)

    def approve(self, incident_id: str, part: str, by: str = "operator") -> dict:
        rep = self.get(incident_id)
        if rep is None:
            raise KeyError(incident_id)
        rep.setdefault("approved", {})[part] = {"by": by, "at": datetime.now(UTC).isoformat(timespec="seconds")}
        self.save(rep)
        return rep

    async def draft(self, agent, inc: dict) -> dict:
        """One Gemini call. Facts and applicability come from our data and rules, not from the model."""
        appl = applicability(inc)
        facts = {
            "incident": {k: inc.get(k) for k in ("id", "kind", "severity", "title", "site", "stations", "opened",
                                                 "recovered", "status", "ttr_s", "metrics")},
            "timeline": [{"time": t["time"], "type": t["type"], "actor": t["actor"], "text": t["text"]}
                         for t in inc["timeline"]],
            "applicable": {"NIS2": appl["nis2"]["applies"], "CRA": appl["cra"]["applies"]},
        }
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": json.dumps(facts, separators=(",", ":"))}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 900, "responseMimeType": "application/json",
                                 "responseSchema": SCHEMA,
                                 "thinkingConfig": ({"thinkingLevel": "minimal"} if agent.model.startswith("gemini-3")
                                                    else {"thinkingBudget": 0})},
        }
        if not agent._budget_ok():
            raise RuntimeError("agent budget reached or paused; try later")
        agent.calls_recent.append(agent.clock())
        agent.calls_today += 1
        try:
            res = await agent.http.post(f"https://generativelanguage.googleapis.com/v1beta/models/{agent.model}"
                                        ":generateContent", json=body, timeout=45,
                                        headers={"x-goog-api-key": agent._key})
        except httpx.HTTPError as exc:
            raise RuntimeError(f"request failed: {exc.__class__.__name__}") from None
        if res.status_code >= 400:
            raise RuntimeError(f"Gemini HTTP {res.status_code}" + (" (busy, try again)" if res.status_code in (500, 503) else ""))
        data = res.json()
        usage = data.get("usageMetadata", {})
        agent.tokens_in += usage.get("promptTokenCount", 0)
        agent.tokens_out += usage.get("candidatesTokenCount", 0)
        try:
            doc = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
        except (KeyError, IndexError, json.JSONDecodeError):
            raise RuntimeError("unreadable answer from Gemini") from None
        report = {"incident_id": inc["id"], "title": inc["title"], "generated": datetime.now(UTC).isoformat(timespec="seconds"),
                  "model": agent.model, "applicability": appl, "doc": doc, "approved": {},
                  "tokens": [usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0)]}
        self.save(report)
        return report
