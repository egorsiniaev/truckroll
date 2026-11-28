"""Daily NIS2 (Cyberbeveiligingswet) and CRA report, built from rules and our own records. No AI, nothing sent.

NIS2: which incidents of the day were significant and what is due when, the area outage and what it cost, and the
patch-management evidence (Art. 21(2)(e): vulnerability handling) from the firmware rollout.
CRA: the firmware we shipped that day as a manufacturer: security fixes, actively exploited vulnerabilities (early
warning 24 h, notification 72 h, final report 14 days after the fix is out), how fast fixes reached the fleet, and
failed updates with their rollbacks.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping
from datetime import datetime, timedelta

from control.firmware import TZ, parse
from control.reports import applicability, nis2_deadlines

SIGNIFICANT_OUTAGE_MIN = 30


def _t(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _cra_deadlines(aware: datetime, final_days: int) -> dict:
    return {"early_warning": (aware + timedelta(hours=24)).isoformat(),
            "notification": (aware + timedelta(hours=72)).isoformat(),
            "final_report": (aware + timedelta(days=final_days)).isoformat()}


def _outage_section(outage: Mapping | None, incidents: list[Mapping]) -> dict | None:
    if not outage:
        return None
    start = _t(outage["started"])
    end = _t(outage["ended"]) if outage.get("ended") else None
    linked = next((i for i in incidents if i["kind"] == "site_outage" and i["site"] == outage["site"]
                   and start - timedelta(minutes=2) <= _t(i["opened"]) <= (end or start + timedelta(hours=1))), None)
    significant = outage["minutes"] >= SIGNIFICANT_OUTAGE_MIN
    why = (f"the whole site was unavailable for {outage['minutes']:.0f} min and lost about €{outage['loss_eur']:.0f} "
           f"in revenue") if significant else f"site down {outage['minutes']:.0f} min, under {SIGNIFICANT_OUTAGE_MIN} min"
    return {**outage, "significant": significant, "why": why, "incident_id": linked["id"] if linked else None,
            "deadlines": nis2_deadlines(_t(linked["opened"]) if linked else start) if significant else {}}


def _patching(events: list[Mapping]) -> dict:
    count = lambda kind: sum(1 for e in events if e["type"] == kind)  # noqa: E731
    ok, failed = count("installed"), count("install_failed")
    installs = [e.get("minutes", 0) for e in events if e["type"] == "installed"]
    by_kind: dict[str, int] = {}
    for e in events:
        if e["type"] == "installed":
            by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
    return {"releases": count("release"), "installs_ok": ok, "installs_failed": failed,
            "success_rate": round(ok / (ok + failed), 3) if ok + failed else None, "by_kind": by_kind,
            "rollbacks": count("rolled_back"), "halted": [e["version"] for e in events if e["type"] == "release_halted"],
            "reverted": count("reverted"), "maintenance_min": round(sum(installs), 1)}


def nis2(incidents: list[Mapping], outage: Mapping | None, events: list[Mapping], money: Mapping | None,
         uptime: float | None) -> dict:
    by_kind: dict[str, int] = {}
    significant = []
    for inc in incidents:
        by_kind[inc["kind"]] = by_kind.get(inc["kind"], 0) + 1
        a = applicability(inc)["nis2"]
        if a["applies"]:
            significant.append({"id": inc["id"], "title": inc["title"], "site": inc["site"], "why": a["why"],
                                "status": inc["status"], "deadlines": a["deadlines"]})
    area = _outage_section(outage, incidents)
    if area and area["significant"] and not any(s["id"] == area["incident_id"] for s in significant):
        significant.append({"id": area["incident_id"] or "area-outage", "title": f"Area outage at {area['site']}",
                            "site": area["site"], "why": area["why"], "status": "recovered" if area.get("ended") else "open",
                            "deadlines": area["deadlines"]})
    fleet = (money or {}).get("fleet", {})
    return {
        "verdict": "reportable" if significant else "nothing to report",
        "incidents": len(incidents), "by_kind": by_kind, "significant": significant, "area_outage": area,
        "financial": {"lost_revenue_eur": fleet.get("lost_eur"), "field_service_eur": fleet.get("service_eur"),
                      "outage_loss_eur": area["loss_eur"] if area else None,
                      "revenue_eur": fleet.get("revenue_eur"), "margin_eur": fleet.get("margin_eur")},
        "uptime": uptime, "patching": _patching(events),
    }


def _time_to_patch(events: list[Mapping], release: Mapping) -> list[float]:
    """Minutes from a fix's release to each charger running it or anything newer."""
    cut = _t(release["time"])
    need = parse(release["version"])
    seen: dict[str, float] = {}
    for e in events:
        if e["type"] == "installed" and e["station"] not in seen and parse(e["to"]) >= need and _t(e["time"]) >= cut:
            seen[e["station"]] = round((_t(e["time"]) - cut).total_seconds() / 60, 1)
    return list(seen.values())


def cra(events: list[Mapping], versions: Mapping[str, str], releases: list[Mapping]) -> dict:
    shipped = [e for e in events if e["type"] == "release"]
    fixes = []
    halted = {r["version"] for r in releases if r.get("status") == "halted"}
    for r in (e for e in shipped if e.get("security")):
        mins = _time_to_patch(events, r)
        fixes.append({"version": r["version"], "advisory": r["advisory"], "severity": r["security"],
                      "exploited": r["exploited"], "released": r["time"], "patched": len(mins),
                      "halted": r["version"] in halted,
                      "median_min": statistics.median(mins) if mins else None,
                      "deadlines": _cra_deadlines(_t(r["time"]), 14) if r["exploited"] else {}})
    exploited = [f for f in fixes if f["exploited"]]
    fixes_ever = [r for r in releases if r.get("security") and r.get("status") != "halted"]
    newest_fix = max((r["version"] for r in fixes_ever), key=parse, default=None)
    exposed = sorted(s for s, v in versions.items() if newest_fix and parse(v) < parse(newest_fix))
    latest = max((r["version"] for r in releases if r.get("status") != "halted"), key=parse, default=None)
    on_latest = sum(1 for v in versions.values() if v == latest)
    return {
        "verdict": "reportable" if exploited else "nothing to report",
        "why": (f"{len(exploited)} actively exploited vulnerabilit{'y' if len(exploited) == 1 else 'ies'} fixed: early "
                f"warning to the CSIRT via the ENISA single reporting platform within 24 h") if exploited
        else "no actively exploited vulnerability and no severe incident affecting product security",
        "releases": [{k: e.get(k) for k in ("time", "version", "bump", "notes", "security", "advisory", "exploited")}
                     for e in shipped],
        "security_fixes": fixes, "exploited": exploited,
        "fleet": {"stations": len(versions), "latest": latest, "on_latest": on_latest,
                  "newest_security_fix": newest_fix, "exposed": len(exposed), "exposed_stations": exposed[:20]},
        "updates": _patching(events),
    }


def daily(day: str, incidents: list[Mapping], events: list[Mapping], outage: Mapping | None,
          versions: Mapping[str, str], releases: list[Mapping], money: Mapping | None, uptime: float | None,
          generated: str, preliminary: bool) -> dict:
    mine = [i for i in incidents if i.get("opened") and _t(i["opened"]).astimezone(TZ).strftime("%Y-%m-%d") == day]
    n, c = nis2(mine, outage, events, money, uptime), cra(events, versions, releases)
    return {"day": day, "generated": generated, "preliminary": preliminary, "nis2": n, "cra": c,
            "headline": f"NIS2: {n['verdict']} ({len(n['significant'])} significant of "
                        f"{_count(n['incidents'], 'incident')}). CRA: {c['verdict']} "
                        f"({_count(len(c['security_fixes']), 'security fix', 'security fixes')} in "
                        f"{_count(len(c['releases']), 'release')})."}


def _count(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else many or one + 's'}"

