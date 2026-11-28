"""Once a day, one site loses power and its line for 30 minutes: an area outage, for the NIS2 report.

The planner picks a site and a daytime moment each local day, starts the outage scenario when nothing else is
running, and records what it cost: revenue lost to drivers who were turned away, gave up or were cut off.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from control.scenarios import AREA_OUTAGE_S, area_outage_id

TZ = ZoneInfo("Europe/Amsterdam")
WINDOW_H = (9, 19)  # starts between 09:00 and 19:00 local, when drivers are around and the loss is real
LATEST_START_H = 20  # a service started after this skips today's outage
KEEP_DAYS = 14


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).isoformat(timespec="seconds")


@dataclass
class OutagePlanner:
    sites: list[str]
    runner: object
    lost_eur: Callable[[str], float]  # today's lost revenue at a site so far
    rng: random.Random = field(default_factory=random.Random)
    clock: Callable[[], float] = time.time
    enabled: bool = True
    plan: dict = field(default_factory=dict)  # today: day, site, at, then started/ended/loss once it runs
    log: list[dict] = field(default_factory=list)  # finished outages, newest last

    def _plan_day(self, now: float) -> None:
        local = datetime.fromtimestamp(now, TZ)
        day = local.strftime("%Y-%m-%d")
        if self.plan.get("day") == day:
            return
        start = local.replace(hour=WINDOW_H[0], minute=0, second=0, microsecond=0)
        at = start + timedelta(seconds=self.rng.uniform(0, (WINDOW_H[1] - WINDOW_H[0]) * 3600))
        if at.timestamp() < now:  # service started late in the day
            at = local + timedelta(minutes=self.rng.uniform(5, 30))
        skip = at.hour >= LATEST_START_H
        self.plan = {"day": day, "site": self.rng.choice(self.sites), "at": at.timestamp(), "skipped": skip}

    def tick(self, stations: list[Mapping]) -> str | None:
        """Starts or finishes today's outage. Returns a feed text when something happened."""
        now = self.clock()
        p = self.plan
        if p.get("started") and not p.get("ended"):
            running = self.runner.current is not None and self.runner.run_id == p.get("run_id")
            if running:
                return None
            return self._finish(now)
        self._plan_day(now)
        p = self.plan
        if not self.enabled or p.get("skipped") or p.get("started") or now < p["at"] or not stations:
            return None
        if self.runner.current is not None:
            return None  # wait for the running scenario to end
        self.runner.start(area_outage_id(p["site"]), list(stations))
        p.update(started=now, run_id=self.runner.run_id, lost_before=self.lost_eur(p["site"]))
        return f"area outage at {p['site']}: power and line down for {AREA_OUTAGE_S // 60} min"

    def _finish(self, now: float) -> str:
        p = self.plan
        loss = max(0.0, self.lost_eur(p["site"]) - p.get("lost_before", 0.0))
        minutes = round((now - p["started"]) / 60, 1)
        p.update(ended=now, loss_eur=round(loss, 2))
        self.log = [*self.log, self.record(p, minutes)][-KEEP_DAYS:]
        return f"area outage at {p['site']} over after {minutes:.0f} min; lost revenue about €{loss:.0f}"

    @staticmethod
    def record(p: Mapping, minutes: float) -> dict:
        return {"day": p["day"], "site": p["site"], "run_id": p.get("run_id"), "started": _iso(p["started"]),
                "ended": _iso(p["ended"]), "minutes": minutes, "loss_eur": p.get("loss_eur", 0.0),
                "note": p.get("note")}

    def for_day(self, day: str) -> dict | None:
        if self.plan.get("day") == day and self.plan.get("started") and not self.plan.get("ended"):
            now = self.clock()
            return {"day": day, "site": self.plan["site"], "started": _iso(self.plan["started"]), "ended": None,
                    "minutes": round((now - self.plan["started"]) / 60, 1),
                    "loss_eur": round(max(0.0, self.lost_eur(self.plan["site"]) - self.plan.get("lost_before", 0.0)), 2),
                    "note": "still running"}
        return next((r for r in reversed(self.log) if r["day"] == day), None)

    def status(self) -> dict:
        p = self.plan
        state = "skipped" if p.get("skipped") else "done" if p.get("ended") else "running" if p.get("started") \
            else "planned" if p else "none"
        return {"enabled": self.enabled, "state": state, "site": p.get("site"),
                "at": _iso(p["at"]) if p.get("at") else None, "last": self.log[-1] if self.log else None}

    # ---- survive restarts --------------------------------------------------------------
    def to_state(self) -> dict:
        return {"plan": self.plan, "log": self.log}

    def from_state(self, data: Mapping) -> None:
        self.plan = dict(data.get("plan", {}))
        self.log = list(data.get("log", []))
        if self.plan.get("started") and not self.plan.get("ended"):
            # The restart reset every proxy, so the outage ended when the service went down.
            self.plan["note"] = "cut short by a service restart"
            self._finish(self.clock())
