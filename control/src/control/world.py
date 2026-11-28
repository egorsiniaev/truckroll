"""Real-world mode: local time sets demand; live weather and prices set derates and network trouble."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

TZ = ZoneInfo("Europe/Amsterdam")
COLD_C = 3.0
COLD_DERATE = 0.75
STORM_GUST_KMH = 60.0
STORM_LATENCY_MS = 1500
FLAT_SHARE = 0.6
PRICE_DERATE = 0.5  # flexible depot fleet charges at half power in the day's most expensive slots

# Demand share by local hour range [start, end), with the reason a person would give, per kind of day.
WEEKDAY: dict[str, tuple[tuple[int, int, float, str], ...]] = {
    "depot": ((0, 6, 0.9, "overnight fleet charging"), (6, 9, 0.3, "vans leaving for routes"),
              (9, 17, 0.2, "vans out on routes"), (17, 20, 0.6, "vans returning"), (20, 24, 0.9, "overnight fleet charging")),
    "retail": ((0, 8, 0.05, "store closed"), (8, 10, 0.3, "early shoppers"), (10, 18, 0.8, "shopping hours"),
               (18, 21, 0.6, "evening shoppers"), (21, 24, 0.1, "store closed")),
    "highway": ((0, 6, 0.15, "night traffic"), (6, 10, 0.6, "morning commute"), (10, 16, 0.5, "daytime traffic"),
                (16, 20, 0.9, "evening rush hour"), (20, 24, 0.3, "late traffic")),
    "office": ((0, 7, 0.0, "office closed"), (7, 9, 0.8, "staff arriving"), (9, 16, 0.6, "workday"),
               (16, 18, 0.3, "staff leaving"), (18, 24, 0.05, "office closed")),
}
SATURDAY: dict[str, tuple[tuple[int, int, float, str], ...]] = {
    "depot": ((0, 8, 0.5, "weekend: most vans parked, topping up"), (8, 16, 0.25, "weekend: a few vans out"),
              (16, 24, 0.4, "weekend: vans back, topping up")),
    "retail": ((0, 8, 0.05, "store closed"), (8, 10, 0.4, "Saturday early shoppers"), (10, 17, 1.0, "Saturday shopping peak"),
               (17, 20, 0.6, "Saturday late shoppers"), (20, 24, 0.1, "store closed")),
    "highway": ((0, 7, 0.1, "night traffic"), (7, 10, 0.35, "quiet weekend morning"), (10, 17, 0.75, "weekend trips"),
                (17, 21, 0.5, "weekend evening traffic"), (21, 24, 0.25, "late traffic")),
    "office": ((0, 24, 0.03, "weekend: office closed"),),
}
SUNDAY: dict[str, tuple[tuple[int, int, float, str], ...]] = {
    "depot": ((0, 16, 0.25, "Sunday: vans parked"), (16, 24, 0.7, "Sunday evening: fleet charging for Monday")),
    "retail": ((0, 12, 0.05, "Sunday: store closed"), (12, 18, 0.7, "Sunday shopping (12-18)"),
               (18, 24, 0.05, "store closed")),
    "highway": ((0, 9, 0.1, "quiet Sunday morning"), (9, 15, 0.5, "Sunday day trips"),
                (15, 21, 0.85, "Sunday return traffic"), (21, 24, 0.3, "late traffic")),
    "office": ((0, 24, 0.0, "Sunday: office closed"),),
}
DEMAND_PROFILES = WEEKDAY  # kept for callers that only know weekdays
BY_DAY = {"weekday": WEEKDAY, "saturday": SATURDAY, "sunday": SUNDAY}


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (h + m - 7 * ((a + 11 * h + 22 * m) // 451) + 114)
    return date(year, n // 31, n % 31 + 1)


def nl_holidays(year: int) -> dict[date, str]:
    """Dutch public holidays most people have off; King's Day moves to the 26th when the 27th is a Sunday."""
    easter = _easter(year)
    kings = date(year, 4, 27)
    if kings.weekday() == 6:
        kings = date(year, 4, 26)
    days = {date(year, 1, 1): "New Year's Day", easter: "Easter", easter + timedelta(days=1): "Easter Monday",
            kings: "King's Day", easter + timedelta(days=39): "Ascension Day", easter + timedelta(days=49): "Whit Sunday",
            easter + timedelta(days=50): "Whit Monday", date(year, 12, 25): "Christmas",
            date(year, 12, 26): "Boxing Day"}
    if year % 5 == 0:
        days[date(year, 5, 5)] = "Liberation Day"
    return days


def day_type(d: date) -> tuple[str, str | None]:
    """("weekday" | "saturday" | "sunday", holiday name). Public holidays run like a Sunday."""
    holiday = nl_holidays(d.year).get(d)
    if holiday:
        return "sunday", holiday
    return ("saturday" if d.weekday() == 5 else "sunday" if d.weekday() == 6 else "weekday"), None


def demand_entry(profile: str, hour: int, kind: str = "weekday") -> tuple[float, str]:
    for start, end, share, reason in BY_DAY.get(kind, WEEKDAY).get(profile, ()):
        if start <= hour < end:
            return share, reason
    return 1.0, "steady demand"


def demand_share(profile: str, hour: int, kind: str = "weekday") -> float:
    return demand_entry(profile, hour, kind)[0]


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


def local_day(now: datetime | None = None) -> tuple[int, str, str | None]:
    """Local hour, kind of day and holiday name, all from one clock read."""
    local = (now or datetime.now(TZ)).astimezone(TZ)
    return (local.hour, *day_type(local.date()))


class WorldEngine:
    """Applies real-world rules on each tick while enabled; undoes its own changes when disabled."""

    def __init__(self, sites: list[dict], clients, arbiter, enabled: bool = False,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.sites = sites
        self.clock = clock or (lambda: datetime.now(TZ))
        self.clients = clients
        self.arbiter = arbiter
        self.enabled = enabled
        self.override_temp: dict[str, float] = {}  # scenarios can force a temperature per site
        self.rules: list[Rule] = []
        self.targets: dict[str, float] = {}
        self._stormy: set[str] = set()

    def shares(self) -> dict[str, float]:
        """Demand share per site for the demand engine: the local day when on, a flat 60 % when off."""
        if not self.enabled:
            return {s["code"]: FLAT_SHARE for s in self.sites}
        hour, kind, _ = local_day(self.clock())
        return {s["code"]: demand_share(s.get("demand", ""), hour, kind) for s in self.sites}

    def reasons(self) -> dict[str, str]:
        if not self.enabled:
            return {s["code"]: "test demand (real-world mode off)" for s in self.sites}
        hour, kind, holiday = local_day(self.clock())
        prefix = f"{holiday}: " if holiday else ""
        return {s["code"]: prefix + demand_entry(s.get("demand", ""), hour, kind)[1] for s in self.sites}

    def status(self) -> dict:
        hour, kind, holiday = local_day(self.clock())
        return {
            "enabled": self.enabled,
            "hour": hour,
            "day_type": kind,
            "holiday": holiday,
            "demand": self.targets,
            "rules": [r.__dict__ for r in self.rules],
        }

    async def tick(self, snapshot: Mapping, weather: Mapping[str, Mapping], prices: Mapping | None = None) -> list[str]:
        """Returns human-readable changes made, for the event feed."""
        weather = {code: ({**w, "temp_c": self.override_temp[code]} if code in self.override_temp else w)
                   for code, w in {**{s["code"]: {} for s in self.sites}, **weather}.items()}
        self.rules = [r for s in self.sites for r in weather_rules(s, weather.get(s["code"]))]
        self.rules += [r for s in self.sites if (r := price_rule(s, prices)) is not None]
        if not self.enabled:
            return []
        changes: list[str] = []
        hour, kind, _ = local_day(self.clock())
        by_site: dict[str, list[dict]] = {}
        for st in snapshot.get("stations", []):
            by_site.setdefault(st["site"], []).append(st)
        for site in self.sites:
            stations = by_site.get(site["code"], [])
            self.targets[site["code"]] = demand_share(site.get("demand", ""), hour, kind)
            rules = {r.rule: r for r in self.rules if r.site == site["code"]}
            if "cold_derate" in rules:
                changes += await self._apply_derate(site, stations, "cold", rules["cold_derate"].active, COLD_DERATE,
                                                    "cold")
            if "price" in rules:
                changes += await self._apply_derate(site, stations, "price", rules["price"].active, PRICE_DERATE,
                                                    "expensive power")
            changes += await self._apply_storm(site, rules["storm_network"].active)
        return changes

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
        await self.arbiter.clear_source("cold")
        await self.arbiter.clear_source("price")
        for code in sorted(self._stormy):
            site = next(s for s in self.sites if s["code"] == code)
            await self.clients.toxiproxy.set_latency(site["proxy"], 0)
        self._stormy.clear()
