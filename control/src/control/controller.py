"""Site controller: keeps each site under its grid cap plus live solar by sharing power across chargers."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

SOURCE = "site-controller"
HEADROOM = 0.95  # aim slightly below the cap so meter noise doesn't trip it
RELEASE_BELOW = 0.7  # lift limits only when demand is well under the cap...
RELEASE_AFTER_S = 60  # ...for this long, to avoid flapping
RESHARE_CHANGE = 0.05


@dataclass
class SiteState:
    active: bool = False
    share_w: float = 0.0
    below_since: float | None = None
    peak_w: float = 0.0


class SiteController:
    def __init__(self, sites: list[dict], arbiter, clock: Callable[[], float] = time.monotonic) -> None:
        self.sites = sites
        self.arbiter = arbiter
        self.clock = clock
        self.state: dict[str, SiteState] = {s["code"]: SiteState() for s in sites}

    def status(self) -> dict:
        return {code: {"active": st.active, "share_w": round(st.share_w)} for code, st in self.state.items()}

    async def tick(self, snapshot: Mapping) -> list[dict]:
        """Returns transitions: {"site", "kind": "limited"|"reshared"|"released", ...} for the incident log."""
        out = []
        by_code = {s["code"]: s for s in snapshot.get("sites", [])}
        for site in self.sites:
            row = by_code.get(site["code"])
            if row is None:
                continue
            stations = [s for s in snapshot.get("stations", []) if s["site"] == site["code"] and s.get("connected")]
            charging = [s for s in stations if s["state"] == "charging"]
            # Solar and the battery's spare discharge power both add to what the chargers may draw.
            available = site["cap_w"] + (row.get("solar_w") or 0.0) + (row.get("battery_support_w") or 0.0)
            demand = row["power_w"]
            st = self.state[site["code"]]
            st.peak_w = max(st.peak_w, demand) if st.active else demand
            if demand > available:
                share = available * HEADROOM / max(1, len(charging))
                first = not st.active
                if first or abs(share - st.share_w) / max(st.share_w, 1) > RESHARE_CHANGE:
                    await self.arbiter.set_many(SOURCE, {s["id"]: share for s in stations})
                    st.active, st.share_w, st.below_since = True, share, None
                    out.append({"site": site["code"], "kind": "limited" if first else "reshared", "demand_w": demand,
                                "available_w": available, "share_w": share, "stations": len(stations)})
            elif st.active:
                if demand < available * RELEASE_BELOW:
                    if st.below_since is None:
                        st.below_since = self.clock()
                    if self.clock() - st.below_since >= RELEASE_AFTER_S:
                        mine = [sid for sid in self.arbiter.stations_for(SOURCE) if sid.startswith(f"{site['code']}-")]
                        await self.arbiter.clear_source(SOURCE, mine)
                        out.append({"site": site["code"], "kind": "released", "demand_w": demand,
                                    "available_w": available, "peak_w": st.peak_w})
                        self.state[site["code"]] = SiteState()
                else:
                    st.below_since = None
        return out
