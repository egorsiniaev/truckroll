"""Shift handover: what happened since the operator last looked."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from control.fieldservice import VISIT_EUR


def _t(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def handover(since: datetime, snapshot: dict, decisions, ledger_path: Path | None) -> dict:
    incidents = snapshot.get("incidents", [])
    opened = [i for i in incidents if _t(i["opened"]) >= since]
    resolved = [i for i in incidents if i.get("recovered") and _t(i["recovered"]) >= since]
    still_open = [i for i in incidents if i["status"] != "recovered"]
    decided = [d for d in decisions if d.decided_by in ("operator", "timeout")]
    runs = []
    if ledger_path and ledger_path.exists():
        for line in ledger_path.read_text().splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if _t(r["started"]) >= since:
                runs.append(r["scenario"])
    missed = [s["name"] for s in snapshot.get("slo", []) if s["status"] == "missed"]
    money = (snapshot.get("economics") or {}).get("fleet", {})
    return {
        "since": since.isoformat(timespec="minutes"),
        "incidents_opened": len(opened),
        "incidents_resolved": len(resolved),
        "opened_by_kind": dict(Counter(i["kind"] for i in opened).most_common(5)),
        "still_open": [{"title": i["title"], "status": i["status"], "eta": (i.get("eta") or {}).get("basis")}
                       for i in still_open][:8],
        "waiting_for_you": [d["question"] for d in (snapshot.get("decisions") or {}).get("pending", [])],
        "decisions_by_you": sum(1 for d in decided if d.decided_by == "operator"),
        "decisions_by_default": sum(1 for d in decided if d.decided_by == "timeout"),
        "scenarios_run": dict(Counter(runs)),
        "slos_missed": missed,
        "margin_today_eur": money.get("margin_eur"),
        "margin_parts": {"sales_eur": money.get("revenue_eur"), "energy_eur": money.get("cost_eur"),
                         "visits_eur": money.get("service_eur"),
                         "visits": max(money.get("visits") or 0, round((money.get("service_eur") or 0) / VISIT_EUR)),
                         "lost_eur": money.get("lost_eur")},
        "technicians_out": len(snapshot.get("field_jobs", [])),
    }
