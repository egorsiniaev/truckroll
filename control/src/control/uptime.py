"""Time-weighted uptime: the share of connector-time that was usable, since local midnight.

A connector counts as up while its station is connected and the connector is in a usable state (free, preparing,
charging, suspended, finishing). Faulted, unavailable or offline time is downtime. This is how charging uptime is
reported in practice; a snapshot of "working right now" is not uptime.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from control.state import USABLE

TZ = ZoneInfo("Europe/Amsterdam")


@dataclass
class Counter:
    up_s: float = 0.0
    total_s: float = 0.0

    @property
    def ratio(self) -> float | None:
        return round(self.up_s / self.total_s, 5) if self.total_s else None


@dataclass
class UptimeTracker:
    sites: list[str]
    day: str = ""
    since: str = ""
    per_site: dict[str, Counter] = field(default_factory=dict)

    def accrue(self, stations: list[Mapping], dt_s: float, now: datetime) -> None:
        local = now.astimezone(TZ)
        day = local.strftime("%Y-%m-%d")
        if day != self.day:
            # A new day, or the first sample since the service started.
            self.day, self.since = day, ("00:00" if self.day else local.strftime("%H:%M"))
            self.per_site = {code: Counter() for code in self.sites}
        for st in stations:
            counter = self.per_site.setdefault(st["site"], Counter())
            for c in st.get("connectors", []):
                counter.total_s += dt_s
                if st.get("connected") and c["status"] in USABLE:
                    counter.up_s += dt_s

    def summary(self) -> dict:
        fleet = Counter(sum(c.up_s for c in self.per_site.values()), sum(c.total_s for c in self.per_site.values()))
        downtime_min = {code: round((c.total_s - c.up_s) / 60, 1) for code, c in self.per_site.items()}
        return {"since": self.since, "fleet": fleet.ratio,
                "sites": {code: c.ratio for code, c in self.per_site.items()},
                "connector_downtime_min": downtime_min}
