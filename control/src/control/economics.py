"""Money: buy energy at the day-ahead price plus fees, take solar for free, sell at each site's tariff.

Simple on purpose, and every number is visible on the page. Batteries come later.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Amsterdam")
GRID_FEES_EUR_KWH = 0.11  # network tariff, taxes and levies on top of the spot price (rough NL business level)
FALLBACK_SPOT_EUR_MWH = 100.0
PASS_THROUGH = 0.6  # share of a price spike passed on to drivers when dynamic pricing is on
MAX_TARIFF_FACTOR = 1.6
DISCOUNT_PASS_THROUGH = 0.8  # share of a price dip passed on as a discount
MIN_TARIFF_FACTOR = 0.6
SOLAR_DISCOUNT = 0.10  # extra discount while solar covers at least half of the site's load
SOLAR_DISCOUNT_FROM = 0.5
MAX_DEMAND_FACTOR = 1.8


@dataclass(frozen=True)
class Commercial:
    """Per demand profile: what drivers pay and how much they care."""

    tariff_eur_kwh: float
    elasticity: float  # how strongly arrivals fall when the tariff rises above base
    public: bool  # only public chargers get dynamic prices; fleet and office contracts stay fixed


COMMERCIAL: dict[str, Commercial] = {
    "depot": Commercial(0.32, 0.1, False),
    "retail": Commercial(0.49, 1.2, True),
    "highway": Commercial(0.69, 1.5, True),
    "office": Commercial(0.39, 0.3, False),
}


def commercial(site: Mapping) -> Commercial:
    return COMMERCIAL.get(site.get("demand", ""), Commercial(0.45, 1.0, True))


def tariff_factor(prices: Mapping | None, solar_share: float = 0.0) -> float:
    """Multiplier on the base tariff: up in price spikes, down in cheap hours and when solar covers the site."""
    factor = 1.0
    if prices and prices.get("available"):
        avg = prices["avg_eur_mwh"] or 1.0
        delta = (prices["now_eur_mwh"] - avg) / avg
        factor = 1 + delta * (PASS_THROUGH if delta > 0 else DISCOUNT_PASS_THROUGH)
    if solar_share >= SOLAR_DISCOUNT_FROM:
        factor -= SOLAR_DISCOUNT
    return min(MAX_TARIFF_FACTOR, max(MIN_TARIFF_FACTOR, factor))


def tariff(site: Mapping, prices: Mapping | None, dynamic: bool, solar_share: float = 0.0) -> float:
    c = commercial(site)
    if not dynamic or not c.public:
        return c.tariff_eur_kwh
    return round(c.tariff_eur_kwh * tariff_factor(prices, solar_share), 3)


def demand_factor(site: Mapping, current_tariff: float) -> float:
    """Drivers compare with what they normally pay: dearer loses some to competitors, cheaper draws extra."""
    c = commercial(site)
    if current_tariff <= 0:
        return 1.0
    return round(min(MAX_DEMAND_FACTOR, (c.tariff_eur_kwh / current_tariff) ** c.elasticity), 3)


@dataclass
class Ledger:
    energy_kwh: float = 0.0
    solar_kwh: float = 0.0
    grid_kwh: float = 0.0
    revenue_eur: float = 0.0
    cost_eur: float = 0.0
    lost_eur: float = 0.0
    service_eur: float = 0.0  # technician visits

    @property
    def margin_eur(self) -> float:
        return self.revenue_eur - self.cost_eur - self.service_eur

    def to_json(self) -> dict:
        return {k: round(v, 2) for k, v in {**self.__dict__, "margin_eur": self.margin_eur}.items()}


@dataclass
class Economics:
    sites: list[dict]
    dynamic: bool = True
    today: dict[str, Ledger] = field(default_factory=dict)
    rates: dict[str, dict] = field(default_factory=dict)
    day: str = ""

    def _roll_day(self, now: datetime) -> None:
        day = now.astimezone(TZ).strftime("%Y-%m-%d")
        if day != self.day:
            self.day = day
            self.today = {s["code"]: Ledger() for s in self.sites}

    solar_share: dict[str, float] = field(default_factory=dict)

    def tariffs(self, prices: Mapping | None) -> dict[str, float]:
        return {s["code"]: tariff(s, prices, self.dynamic, self.solar_share.get(s["code"], 0.0)) for s in self.sites}

    def accrue(self, snapshot_sites: list[Mapping], prices: Mapping | None, dt_s: float, now: datetime) -> None:
        """Integrate power over the last dt seconds into today's money. Battery power: + charging, - discharging."""
        self._roll_day(now)
        spot = prices["now_eur_mwh"] if prices and prices.get("available") else FALLBACK_SPOT_EUR_MWH
        for row in snapshot_sites:
            power = row["power_w"]
            self.solar_share[row["code"]] = min(1.0, (row.get("solar_w") or 0.0) / power) if power > 0 else 0.0
        tariffs = self.tariffs(prices)
        for row in snapshot_sites:
            code = row["code"]
            power_kw = row["power_w"] / 1000
            battery_kw = ((row.get("battery") or {}).get("power_w") or 0.0) / 1000
            solar_kw = min(power_kw + max(0.0, battery_kw), (row.get("solar_w") or 0.0) / 1000)
            grid_kw = max(0.0, power_kw + battery_kw - solar_kw)
            buy = spot / 1000 + GRID_FEES_EUR_KWH
            rev_h, cost_h = power_kw * tariffs[code], grid_kw * buy
            self.rates[code] = {"revenue_eur_h": round(rev_h, 2), "cost_eur_h": round(cost_h, 2),
                                "margin_eur_h": round(rev_h - cost_h, 2), "tariff_eur_kwh": tariffs[code],
                                "buy_eur_kwh": round(buy, 3)}
            led = self.today[code]
            hours = dt_s / 3600
            led.energy_kwh += power_kw * hours
            led.solar_kwh += solar_kw * hours
            led.grid_kwh += grid_kw * hours
            led.revenue_eur += rev_h * hours
            led.cost_eur += cost_h * hours

    def service(self, code: str, eur: float) -> None:
        if code in self.today:
            self.today[code].service_eur += eur

    def lost(self, code: str, kwh: float, prices: Mapping | None) -> None:
        if code in self.today:
            self.today[code].lost_eur += kwh * self.tariffs(prices)[code]

    def summary(self) -> dict:
        total = Ledger()
        for led in self.today.values():
            for k in ("energy_kwh", "solar_kwh", "grid_kwh", "revenue_eur", "cost_eur", "lost_eur", "service_eur"):
                setattr(total, k, getattr(total, k) + getattr(led, k))
        return {"dynamic": self.dynamic, "day": self.day, "fleet": total.to_json(),
                "sites": {c: {**led.to_json(), **self.rates.get(c, {})} for c, led in self.today.items()}}
