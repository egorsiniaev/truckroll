"""Agent eval: join the agent's verdicts with the incident ledger (ground truth it never sees).

For each scenario run: was an incident raised, did the agent triage it, was the root cause category right, and did
it ever say "all clear" about a real fault. Decoys (expected events) count as right when nothing alarming happens
or when the agent calls them expected.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta
from pathlib import Path

from control.scenarios import SCENARIOS

GRACE = timedelta(seconds=120)  # incidents opened shortly after a run ended still belong to it
SITES = {s.id: set(s.sites) for s in SCENARIOS}


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _t(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def evaluate(ledger_path: Path, triage_path: Path) -> dict:
    all_runs = [r for r in _read(ledger_path) if r.get("outcome", "").startswith(("completed", "stopped"))]
    every = sorted(_read(triage_path), key=lambda t: t["opened"])
    # Triages from before 2026-11-25 ~14:00 could see scenario events for fleet-wide incidents (a leak, fixed).
    # Records made after the fix carry a "mode"; only those are scored.
    triages = [t for t in every if "mode" in t]
    # Only runs that started once the agent was live can be scored; earlier ones are reported, not counted.
    agent_live = min((_t(t["triaged"]) for t in triages if "triaged" in t), default=None)
    runs = [r for r in all_runs if agent_live is not None and _t(r["started"]) >= agent_live]
    used: set[str] = set()
    results = []
    for run in runs:
        truth = run["root_cause"].split(":", 1)[0].strip()
        start, end = _t(run["started"]), _t(run["ended"]) + GRACE
        sites = SITES.get(run["scenario"], set())
        # Only incidents that opened during the run (or just after) at its sites belong to it.
        hits = [t for t in triages if start <= _t(t["opened"]) <= end and (t["site"] in sites or t["site"] == "ALL")]
        used |= {t["incident_id"] for t in hits}
        first = hits[0] if hits else None
        if first is None:
            correct = correct_first = truth == "expected"  # nothing alarming about an expected event is right
        else:
            correct = any(t["category"] == truth for t in hits)
            correct_first = first["category"] == truth
        results.append({
            "run_id": run["run_id"], "scenario": run["scenario"], "truth": truth, "incidents_triaged": len(hits),
            "agent_category": first["category"] if first else None, "correct": correct, "correct_first": correct_first,
            "agent_categories": [t["category"] for t in hits],
            "false_all_clear": bool(first and truth != "expected" and first["all_clear"]),
            "seconds_to_summary": first["seconds_after_open"] if first else None,
            "agent_summary": first["summary"] if first else None,
            "mode": first.get("mode", "full") if first else None,
        })
    triaged = [r for r in results if r["agent_category"] is not None]
    latencies = [r["seconds_to_summary"] for r in triaged]
    all_tokens = [(t["tokens_in"], t["tokens_out"]) for t in triages]
    return {
        "runs": len(results),
        "triages_before_leak_fix": len(every) - len(triages),
        "runs_before_agent": len(all_runs) - len(runs),
        "runs_triaged": len(triaged),
        "accuracy": round(sum(r["correct"] for r in triaged) / len(triaged), 3) if triaged else None,
        "accuracy_first": round(sum(r["correct_first"] for r in triaged) / len(triaged), 3) if triaged else None,
        "accuracy_all_runs": round(sum(r["correct"] for r in results) / len(results), 3) if results else None,
        "false_all_clear": sum(r["false_all_clear"] for r in results),
        "median_seconds_to_summary": statistics.median(latencies) if latencies else None,
        "triages_total": len(triages),
        "triages_outside_scenarios": len([t for t in triages if t["incident_id"] not in used]),
        "avg_tokens_in": round(statistics.mean(t[0] for t in all_tokens)) if all_tokens else None,
        "avg_tokens_out": round(statistics.mean(t[1] for t in all_tokens)) if all_tokens else None,
        "by_scenario": _by_scenario(results),
        "by_mode": {mode: {"runs": len(rs), "accuracy": round(sum(r["correct_first"] for r in rs) / len(rs), 3)}
                    for mode in ("full", "blind") if (rs := [r for r in triaged if r["mode"] == mode])},
        "triages_with_queries": sum(1 for t in triages if t.get("queries")),
        "runs_detail": results[-30:],
    }


def _by_scenario(results: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for r in results:
        s = out.setdefault(r["scenario"], {"runs": 0, "correct": 0, "triaged": 0})
        s["runs"] += 1
        s["correct"] += int(r["correct"])
        s["triaged"] += int(r["agent_category"] is not None)
    return out
