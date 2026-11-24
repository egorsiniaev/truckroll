"""Pure functions that turn upstream data into the snapshot the web page renders."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

USABLE = {"Available", "Preparing", "Charging", "SuspendedEV", "SuspendedEVSE", "Finishing"}
ACTIVE = {"Charging", "SuspendedEV", "SuspendedEVSE"}
STATE_PRIORITY = ("offline", "faulted", "charging", "preparing", "available", "unavailable")


def station_state(station: Mapping) -> str:
    """One word per station for colouring: worst connector wins, offline beats everything."""
    if not station.get("connected"):
        return "offline"
    statuses = {c["status"] for c in station.get("connectors", [])}
    if "Faulted" in statuses:
        return "faulted"
    if statuses & ACTIVE:
        return "charging"
    if statuses & {"Preparing", "Finishing"}:
        return "preparing"
    if "Available" in statuses:
        return "available"
    return "unavailable"


def _usable(station: Mapping, connector: Mapping) -> bool:
    return bool(station.get("connected")) and connector["status"] in USABLE


def site_summary(site: Mapping, stations: list[Mapping], proxy: Mapping | None, weather: Mapping | None) -> dict:
    connectors = [(s, c) for s in stations for c in s.get("connectors", [])]
    total = len(connectors) or site["stations"] * site["connectors"]
    usable = sum(_usable(s, c) for s, c in connectors)
    power = sum(c.get("power_w") or 0.0 for _, c in connectors)
    toxics = (proxy or {}).get("toxics", [])
    latency = next((t["attributes"].get("latency", 0) for t in toxics if t.get("type") == "latency"), 0)
    counts: dict[str, int] = {}
    for s in stations:
        state = station_state(s)
        counts[state] = counts.get(state, 0) + 1
    free = sum(1 for st, c in connectors if st.get("connected") and c["status"] == "Available")
    return {
        **site,
        "free": free,
        "connectors_total": total,
        "power_w": round(power, 1),
        "cap_used": round(power / site["cap_w"], 4) if site.get("cap_w") else None,
        "availability": round(usable / total, 4) if total else None,
        "states": counts,
        "network": {"enabled": (proxy or {}).get("enabled", True), "latency_ms": latency, "known": proxy is not None},
        "weather": weather,
        "station_ids": [s["id"] for s in stations],
    }


def build_snapshot(
    sites: list[Mapping],
    stations: list[Mapping],
    proxies: Mapping[str, Mapping],
    weather: Mapping[str, Mapping],
    upstream_errors: Mapping[str, str],
    now_iso: str,
) -> dict:
    by_site: dict[str, list[Mapping]] = {s["code"]: [] for s in sites}
    enriched = []
    for st in stations:
        st = {**st, "state": station_state(st), "power_w": sum(c.get("power_w") or 0.0 for c in st.get("connectors", []))}
        enriched.append(st)
        by_site.setdefault(st["site"], []).append(st)
    site_rows = [
        site_summary(s, by_site.get(s["code"], []), proxies.get(s["proxy"]), weather.get(s["code"])) for s in sites
    ]
    connectors = [(s, c) for s in enriched for c in s.get("connectors", [])]
    total_connectors = len(connectors)
    return {
        "time": now_iso,
        "kpis": {
            "stations_total": len(enriched),
            "stations_online": sum(1 for s in enriched if s.get("connected")),
            "connectors_total": total_connectors,
            "charging": sum(1 for _, c in connectors if c["status"] in ACTIVE),
            "free": sum(1 for s, c in connectors if s.get("connected") and c["status"] == "Available"),
            "faulted": sum(1 for _, c in connectors if c["status"] == "Faulted"),
            "availability": round(sum(_usable(s, c) for s, c in connectors) / total_connectors, 4)
            if total_connectors else None,
            "power_w": round(sum(c.get("power_w") or 0.0 for _, c in connectors), 1),
        },
        "sites": site_rows,
        "stations": enriched,
        "upstream_errors": dict(upstream_errors),
    }


def detect_events(previous: Iterable[Mapping], current: Iterable[Mapping]) -> list[dict]:
    """State changes worth showing in the feed: offline, back online, faulted, fault cleared."""
    before = {s["id"]: s["state"] for s in previous}
    events = []
    for s in current:
        old, new = before.get(s["id"]), s["state"]
        if old is None or old == new:
            continue
        if new == "offline":
            events.append({"level": "error", "station": s["id"], "site": s["site"], "text": "went offline"})
        elif old == "offline":
            events.append({"level": "ok", "station": s["id"], "site": s["site"], "text": "back online"})
        elif new == "faulted":
            codes = sorted({c["error_code"] for c in s["connectors"] if c["status"] == "Faulted"})
            events.append({"level": "error", "station": s["id"], "site": s["site"],
                           "text": f"faulted ({', '.join(codes)})"})
        elif old == "faulted":
            events.append({"level": "ok", "station": s["id"], "site": s["site"], "text": "fault cleared"})
    return events


GROUP_THRESHOLD = 3


def group_events(events: list[dict]) -> list[dict]:
    """Collapse bursts (a site outage, a reconnect storm) into one line per site and change."""
    buckets: dict[tuple[str, str, str], list[dict]] = {}
    for e in events:
        buckets.setdefault((e["site"], e["level"], e["text"]), []).append(e)
    out = []
    for (site, level, text), items in buckets.items():
        if len(items) > GROUP_THRESHOLD:
            out.append({"level": level, "station": None, "site": site, "text": f"{len(items)} stations {text}"})
        else:
            out.extend(items)
    return out
