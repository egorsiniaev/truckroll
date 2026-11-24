"""Real-world mode: local time and live weather drive arrivals, derates and network trouble."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

TZ = ZoneInfo("Europe/Amsterdam")
COLD_C = 3.0
COLD_DERATE = 0.75
STORM_GUST_KMH = 60.0
STORM_LATENCY_MS = 1500
PRICE_DERATE = 0.5  # flexible depot fleet charges at half power in the day's most expensive slots

# Share of stations with arrivals, by local hour range [start, end).
DEMAND_PROFILES: dict[str, tuple[tuple[int, int, float], ...]] = {
    "depot": ((0, 6, 0.9), (6, 9, 0.3), (9, 17, 0.2), (17, 20, 0.6), (20, 24, 0.9)),
    "retail": ((0, 8, 0.05), (8, 10, 0.3), (10, 18, 0.8), (18, 21, 0.6), (21, 24, 0.1)),
    "highway": ((0, 6, 0.15), (6, 10, 0.6), (10, 16, 0.5), (16, 20, 0.9), (20, 24, 0.3)),
    "office": ((0, 7, 0.0), (7, 9, 0.8), (9, 16, 0.6), (16, 18, 0.3), (18, 24, 0.05)),
}


def demand_share(profile: str, hour: int) -> float:
    for start, end, share in DEMAND_PROFILES.get(profile, ()):
        if start <= hour < end:
            return share
    return 1.0


def active_stations(station_ids: list[str], share: float) -> set[str]:
    """Deterministic pick so the same stations stay busy between ticks."""
    ordered = sorted(station_ids)
    return set(ordered[: math.ceil(share * len(ordered))])


def solar_w(site: Mapping, weather: Mapping | None) -> float:
    if not weather or weather.get("radiation_wm2") is None:
        return 0.0
    # 1000 W/m2 is standard test conditions; real yield is lower, 0.8 covers losses.
    return round(site.get("pv_w", 0) * weather["radiation_wm2"] / 1000 * 0.8, 1)


@dataclass(frozen=True)
class Rule:
    site: str
    rule: str
    active: bool
    text: str


def weather_rules(site: Mapping, weather: Mapping | None) -> list[Rule]:
    code = site["code"]
    temp = (weather or {}).get("temp_c")
    gust = (weather or {}).get("gust_kmh")
    cold = site.get("current") == "DC" and temp is not None and temp < COLD_C
    storm = gust is not None and gust >= STORM_GUST_KMH
    rules = [Rule(code, "storm_network", storm,
                  f"gusts {gust} km/h: site network degraded" if storm else "network trouble at gusts of 60 km/h or more")]
    if site.get("current") == "DC":
        rules.insert(0, Rule(code, "cold_derate", cold,
                             f"{temp}°C: DC charging slowed to {int(COLD_DERATE * 100)}% (cold batteries)" if cold
                             else "DC derate below 3°C"))
    return rules


def price_rule(site: Mapping, prices: Mapping | None) -> Rule | None:
    if site.get("demand") != "depot":
        return None
    if not prices or not prices.get("available"):
        return Rule(site["code"], "price", False, "price data not available")
    p = prices["now_eur_mwh"]
    if prices["expensive"]:
        return Rule(site["code"], "price", True,
                    f"€{p:.0f}/MWh is in today's top 20%: depot fleet charges at {int(PRICE_DERATE * 100)}%")
    return Rule(site["code"], "price", False,
                f"€{p:.0f}/MWh; slows the depot at €{prices['expensive_from_eur_mwh']:.0f}/MWh or more")


def local_hour(now: datetime | None = None) -> int:
    return (now or datetime.now(TZ)).astimezone(TZ).hour


class WorldEngine:
    """Applies real-world rules on each tick while enabled; undoes its own changes when disabled."""

    def __init__(self, sites: list[dict], clients, arbiter, enabled: bool = False) -> None:
        self.sites = sites
        self.clients = clients
        self.arbiter = arbiter
        self.enabled = enabled
        self.rules: list[Rule] = []
        self.targets: dict[str, float] = {}
        self._atg_off: set[str] = set()
        self._stormy: set[str] = set()
        self._busy_counts: dict[str, int] = {}

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "hour": local_hour(),
            "demand": self.targets,
            "rules": [r.__dict__ for r in self.rules],
        }

    async def tick(self, snapshot: Mapping, weather: Mapping[str, Mapping], prices: Mapping | None = None) -> list[str]:
        """Returns human-readable changes made, for the event feed."""
        self.rules = [r for s in self.sites for r in weather_rules(s, weather.get(s["code"]))]
        self.rules += [r for s in self.sites if (r := price_rule(s, prices)) is not None]
        if not self.enabled:
            return []
        changes: list[str] = []
        hour = local_hour()
        by_site: dict[str, list[dict]] = {}
        for st in snapshot.get("stations", []):
            by_site.setdefault(st["site"], []).append(st)
        for site in self.sites:
            stations = by_site.get(site["code"], [])
            ids = [s["id"] for s in stations if s.get("connected")]
            share = demand_share(site.get("demand", ""), hour)
            self.targets[site["code"]] = share
            changes += await self._apply_demand(site["code"], ids, active_stations(ids, share))
            rules = {r.rule: r for r in self.rules if r.site == site["code"]}
            if "cold_derate" in rules:
                changes += await self._apply_derate(site, stations, "cold", rules["cold_derate"].active, COLD_DERATE,
                                                    "cold")
            if "price" in rules:
                changes += await self._apply_derate(site, stations, "price", rules["price"].active, PRICE_DERATE,
                                                    "expensive power")
            changes += await self._apply_storm(site, rules["storm_network"].active)
        return changes

    async def _apply_demand(self, code: str, ids: list[str], busy: set[str]) -> list[str]:
        # Re-apply the whole target every tick: idempotent, and it survives a simulator restart.
        idle = [i for i in ids if i not in busy]
        if idle:
            await self.clients.simulator.set_atg_many(idle, False)
        if busy:
            await self.clients.simulator.set_atg_many(sorted(busy), True)
        self._atg_off = (self._atg_off - busy) | set(idle)
        count = len(busy)
        if self._busy_counts.get(code) != count:
            self._busy_counts[code] = count
            return [f"{code}: arrivals at {count}/{len(ids)} stations for this hour"]
        return []

    async def _apply_derate(self, site: Mapping, stations: list[dict], source: str, active: bool, factor: float,
                            reason: str) -> list[str]:
        ids = [st["id"] for st in stations if st.get("connected")]
        had = set(self.arbiter.stations_for(source)) & {st["id"] for st in stations}
        if active:
            todo = [i for i in ids if i not in had]
            if todo:
                await self.arbiter.set_many(source, {i: site["power_w"] * factor for i in todo})
                return [f"{site['code']}: {reason}, {len(todo)} stations limited to {int(factor * 100)}%"]
        elif had:
            await self.arbiter.clear_source(source, sorted(had))
            return [f"{site['code']}: no more {reason}, {len(had)} stations back to full power"]
        return []

    async def _apply_storm(self, site: Mapping, storm: bool) -> list[str]:
        code = site["code"]
        if storm and code not in self._stormy:
            await self.clients.toxiproxy.set_latency(site["proxy"], STORM_LATENCY_MS)
            self._stormy.add(code)
            return [f"{code}: storm, network latency +{STORM_LATENCY_MS} ms"]
        if not storm and code in self._stormy:
            await self.clients.toxiproxy.set_latency(site["proxy"], 0)
            self._stormy.discard(code)
            return [f"{code}: storm over, network latency removed"]
        return []

    async def disable(self) -> None:
        """Undo everything the engine set, so manual control starts from a clean fleet."""
        self.enabled = False
        if self._atg_off:
            await self.clients.simulator.set_atg_many(sorted(self._atg_off), True)
            self._atg_off.clear()
        self._busy_counts.clear()
        await self.arbiter.clear_source("cold")
        await self.arbiter.clear_source("price")
        for code in sorted(self._stormy):
            site = next(s for s in self.sites if s["code"] == code)
            await self.clients.toxiproxy.set_latency(site["proxy"], 0)
        self._stormy.clear()
