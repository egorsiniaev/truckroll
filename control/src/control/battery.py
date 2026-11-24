"""Site batteries: store cheap or solar energy, release it when power is expensive or the site hits its grid cap.

The batteries are virtual (not in the simulator): they change grid draw, energy cost and how much power the site
controller can hand out. Power sign: + charging, - discharging.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

SOC_MIN, SOC_MAX = 0.10, 0.95
EFFICIENCY = 0.90  # round trip; charged energy is reduced by this before it is stored
CHEAP_RANK = 0.25  # charge from the grid in the day's cheapest quarter


@dataclass(frozen=True)
class Spec:
    capacity_kwh: float
    power_kw: float


SPECS: dict[str, Spec] = {
    "depot": Spec(400, 200),
    "retail": Spec(150, 75),
    "highway": Spec(600, 300),
    "office": Spec(100, 50),
}


@dataclass
class Battery:
    spec: Spec
    soc_kwh: float
    power_w: float = 0.0
    mode: str = "idle"
    reason: str = ""

    @property
    def soc(self) -> float:
        return self.soc_kwh / self.spec.capacity_kwh

    def support_w(self) -> float:
        """Power the battery could add right now to cover load above the cap."""
        return self.spec.power_kw * 1000 if self.soc > SOC_MIN + 0.01 else 0.0

    def to_json(self) -> dict:
        return {"capacity_kwh": self.spec.capacity_kwh, "power_kw_max": self.spec.power_kw,
                "soc": round(self.soc, 3), "soc_kwh": round(self.soc_kwh, 1), "power_w": round(self.power_w),
                "mode": self.mode, "reason": self.reason}


def decide(b: Battery, power_w: float, cap_w: float, solar_w: float, prices: Mapping | None) -> tuple[float, str, str]:
    """Return (power W, mode, reason) for the next interval."""
    p_max = b.spec.power_kw * 1000
    room_to_charge = (SOC_MAX - b.soc) * b.spec.capacity_kwh
    room_to_discharge = (b.soc - SOC_MIN) * b.spec.capacity_kwh
    over_cap = power_w - solar_w - cap_w
    if over_cap > 0 and room_to_discharge > 0:
        return -min(p_max, over_cap), "discharging", "covering load above the grid cap"
    surplus = solar_w - power_w
    if surplus > 0 and room_to_charge > 0:
        return min(p_max, surplus), "charging", "storing spare solar"
    available = bool(prices and prices.get("available"))
    if available and prices["expensive"] and room_to_discharge > 0:
        need = max(0.0, power_w - solar_w)
        if need > 0:
            return -min(p_max, need), "discharging", "power is expensive: using stored energy"
    if available and prices["rank"] <= CHEAP_RANK and room_to_charge > 0:
        headroom = cap_w + solar_w - power_w
        if headroom > 0:
            return min(p_max, headroom), "charging", "power is cheap: filling up"
    return 0.0, "idle", "waiting for cheap power or solar" if room_to_charge > 0 else "full"


class BatteryFleet:
    def __init__(self, sites: list[dict], enabled: bool = True, start_soc: float = 0.5) -> None:
        self.enabled = enabled
        self.batteries = {
            s["code"]: Battery(SPECS[s["demand"]], SPECS[s["demand"]].capacity_kwh * start_soc)
            for s in sites if s.get("demand") in SPECS
        }

    def tick(self, snapshot_sites: list[Mapping], prices: Mapping | None, dt_s: float) -> None:
        for row in snapshot_sites:
            b = self.batteries.get(row["code"])
            if b is None:
                continue
            if not self.enabled:
                b.power_w, b.mode, b.reason = 0.0, "off", "batteries switched off"
                continue
            power, mode, reason = decide(b, row["power_w"], row["cap_w"], row.get("solar_w") or 0.0, prices)
            hours = dt_s / 3600
            delta_kwh = power / 1000 * hours * (EFFICIENCY if power > 0 else 1.0)
            new_soc = min(SOC_MAX, max(SOC_MIN, b.soc + delta_kwh / b.spec.capacity_kwh))
            b.soc_kwh = new_soc * b.spec.capacity_kwh
            b.power_w, b.mode, b.reason = power, mode, reason

    def status(self) -> dict[str, dict]:
        return {code: b.to_json() for code, b in self.batteries.items()}

    def support_w(self, code: str) -> float:
        b = self.batteries.get(code)
        return b.support_w() if b is not None and self.enabled else 0.0
