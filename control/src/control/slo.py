"""Service levels from GOALS.md, measured since local midnight (or service start) and saved across restarts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

CAP_TOLERANCE = 1.02  # meter noise
CAP_VIOLATION_S = 60
FLEX_TOLERANCE = 1.05


@dataclass
class SiteSlo:
    over_since: float | None = None
    counted: bool = False
    violations: int = 0
    over_s: float = 0.0
    flex_s: float = 0.0
    flex_ok_s: float = 0.0
    flex_requests: int = 0
    in_request: bool = False


@dataclass
class SloTracker:
    sites: list[str]
    day: str = ""
    per_site: dict[str, SiteSlo] = field(default_factory=dict)

    def accrue(self, snap_sites: list[Mapping], flex_targets: Mapping[str, float], dt_s: float, now: float, day: str) -> None:
        if day != self.day:
            self.day, self.per_site = day, {c: SiteSlo() for c in self.sites}
        for row in snap_sites:
            st = self.per_site.setdefault(row["code"], SiteSlo())
            grid, cap = row.get("grid_w", row["power_w"]), row["cap_w"]
            if grid > cap * CAP_TOLERANCE:
                st.over_s += dt_s
                st.over_since = st.over_since if st.over_since is not None else now
                if not st.counted and now - st.over_since >= CAP_VIOLATION_S:
                    st.violations, st.counted = st.violations + 1, True
            else:
                st.over_since, st.counted = None, False
            target = flex_targets.get(row["code"])
            if target:
                st.flex_requests += 0 if st.in_request else 1
                st.in_request = True
                st.flex_s += dt_s
                st.flex_ok_s += dt_s if grid <= target * FLEX_TOLERANCE else 0.0
            else:
                st.in_request = False

    def summary(self, uptime: Mapping, demand: Mapping[str, Mapping]) -> list[dict]:
        ok = sum(d.get("starts_ok", 0) for d in demand.values())
        failed = sum(d.get("starts_failed", 0) for d in demand.values())
        violations = sum(s.violations for s in self.per_site.values())
        flex_s = sum(s.flex_s for s in self.per_site.values())
        flex = sum(s.flex_ok_s for s in self.per_site.values()) / flex_s if flex_s else None

        def row(id_, name, value, target, higher_better, unit, detail):
            if value is None:
                status = "no data"
            elif higher_better:
                status = "met" if value >= target else ("close" if value >= target - 0.01 else "missed")
            else:
                status = "met" if value <= target else "missed"
            return {"id": id_, "name": name, "value": value, "target": target, "unit": unit, "status": status,
                    "detail": detail}

        return [
            row("uptime", "Uptime, as experienced", uptime.get("fleet"), 0.99, True, "ratio",
                "all connector-time, injected faults included"),
            row("uptime_excl", "Uptime, excluding injected faults", uptime.get("fleet_excluding_injected"), 0.99,
                True, "ratio", "leaves out sites and chargers a running scenario affects"),
            row("starts", "Session start success", ok / (ok + failed) if ok + failed else None, 0.98, True, "ratio",
                f"{ok} started, {failed} failed to start"),
            row("cap", "Grid cap compliance", violations, 0, False, "count",
                f"episodes over the cap for more than {CAP_VIOLATION_S} s; "
                f"{sum(s.over_s for s in self.per_site.values()) / 60:.1f} min over in total"),
            row("flex", "Flex delivery", flex, 0.95, True, "ratio",
                f"time under the grid operator's limit during {sum(s.flex_requests for s in self.per_site.values())} request(s)"),
        ]

    def to_state(self) -> dict:
        return {"day": self.day, "sites": {c: s.__dict__ for c, s in self.per_site.items()}}

    def from_state(self, data: dict) -> None:
        self.day = data.get("day", "")
        self.per_site = {c: SiteSlo(**{**v, "over_since": None, "counted": False}) for c, v in data.get("sites", {}).items()}
