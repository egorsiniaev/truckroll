"""The hub: one poll loop that reads the world, runs the automatic parts, and publishes one snapshot."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from control.autopilot import Autopilot
from control.battery import BatteryFleet
from control.clients import CsmsClient, SimulatorClient, ToxiproxyClient, UpstreamError, WeatherClient
from control.controller import SiteController
from control.daylight import daylight
from control.demand import DemandEngine
from control.economics import Economics, demand_factor
from control.incidents import IncidentManager
from control.limits import SOURCE_LABELS, LimitArbiter
from control.prices import PriceClient, PriceError, summarize
from control.scenarios import Ledger, Runner
from control.state import build_snapshot, detect_events, group_events
from control.uptime import UptimeTracker
from control.world import WorldEngine, solar_w

log = logging.getLogger(__name__)

POLL_S = 2.0
CONTROLLER_EVERY_S = 10
DEMAND_EVERY_S = 5
WORLD_EVERY_S = 60
WEATHER_EVERY_S = 600
PRICES_EVERY_S = 3600
PRICES_RETRY_S = 600
SAVE_EVERY_S = 60
MAX_EVENTS = 80


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class Clients:
    csms: CsmsClient
    toxiproxy: ToxiproxyClient
    simulator: SimulatorClient
    weather: WeatherClient | None
    prices: PriceClient | None = None


@dataclass
class Hub:
    sites: list[dict]
    clients: Clients
    ledger_path: Path | None = None
    world_default: bool = False
    autopilot_default: bool = False
    grafana_url: str | None = None
    dynamic_pricing_default: bool = True
    batteries_default: bool = True
    id_tags: list = field(default_factory=lambda: [f"TR-{n:04d}" for n in range(1, 101)])
    snapshot: dict = field(default_factory=dict)
    events: deque = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    weather: dict = field(default_factory=dict)
    weather_error: str | None = None
    prices: dict = field(default_factory=lambda: {"available": False})
    prices_error: str | None = None
    _slots: list = field(default_factory=list)
    _due: dict = field(default_factory=dict)
    _reconciled: bool = False
    _last_poll: float = 0.0

    def __post_init__(self) -> None:
        self.limits = LimitArbiter(self.clients.csms)
        self.world = WorldEngine(self.sites, self.clients, self.limits, enabled=self.world_default)
        self.runner = Runner(self.clients, self.sites, Ledger(self.ledger_path), self._scenario_event,
                             limits=self.limits)
        self.controller = SiteController(self.sites, self.limits)
        self.incidents = IncidentManager(self.sites, self.clients)
        self.autopilot = Autopilot(self.runner, enabled=self.autopilot_default)
        self.economics = Economics(self.sites, dynamic=self.dynamic_pricing_default)
        self.uptime = UptimeTracker([s["code"] for s in self.sites])
        self.batteries = BatteryFleet(self.sites, enabled=self.batteries_default)
        self.state_path = self.ledger_path.with_name("state.json") if self.ledger_path else None
        self._load_state()
        self.demand = DemandEngine(self.sites, self.clients, self.id_tags,
                                   on_lost=lambda site, kwh: self.economics.lost(site, kwh, self.prices))

    # ---- survive restarts: uptime, today's money, battery charge -------------------
    def _save_state(self) -> None:
        if not self.state_path or not self.uptime.day:
            return
        data = {
            "day": self.uptime.day, "uptime_since": self.uptime.since,
            "uptime": {c: [v.up_s, v.total_s] for c, v in self.uptime.per_site.items()},
            "economics_day": self.economics.day,
            "economics": {c: led.__dict__ for c, led in self.economics.today.items()},
            "battery_soc_kwh": {c: b.soc_kwh for c, b in self.batteries.batteries.items()},
        }
        try:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(self.state_path)
        except OSError as exc:
            log.warning("state save failed: %s", exc)

    def _load_state(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("state load failed, starting fresh: %s", exc)
            return
        from control.economics import Ledger
        from control.uptime import Counter
        today = datetime.now(UTC).astimezone(self.uptime_tz()).strftime("%Y-%m-%d")
        if data.get("day") == today:
            self.uptime.day, self.uptime.since = today, data.get("uptime_since", "")
            self.uptime.per_site = {c: Counter(*v) for c, v in data.get("uptime", {}).items()}
        if data.get("economics_day") == today:
            self.economics.day = today
            self.economics.today = {c: Ledger(**v) for c, v in data.get("economics", {}).items()}
        for code, soc in data.get("battery_soc_kwh", {}).items():
            if code in self.batteries.batteries:
                self.batteries.batteries[code].soc_kwh = soc
        log.info("restored uptime, money and battery state from %s", self.state_path)

    @staticmethod
    def uptime_tz():
        from control.uptime import TZ
        return TZ

    # ---- feed --------------------------------------------------------------
    def add_event(self, level: str, text: str, site: str | None = None, station: str | None = None,
                  kind: str = "log") -> None:
        self.events.appendleft({"time": _now_iso(), "level": level, "text": text, "site": site,
                                "station": station, "kind": kind})

    def _scenario_event(self, level: str, text: str) -> None:
        self.add_event(level, text, kind="scenario")

    def _due_now(self, name: str, every_s: float) -> bool:
        now = time.monotonic()
        if now >= self._due.get(name, 0):
            self._due[name] = now + every_s
            return True
        return False

    # ---- external data ------------------------------------------------------------
    async def refresh_weather(self) -> None:
        if self.clients.weather is None or (self.weather and not self._due_now("weather", WEATHER_EVERY_S)):
            return
        self._due["weather"] = time.monotonic() + WEATHER_EVERY_S
        try:
            rows = await self.clients.weather.current([(s["lat"], s["lon"]) for s in self.sites])
            self.weather = {s["code"]: {**row, "stale": False} for s, row in zip(self.sites, rows, strict=True)}
            self.weather_error = None
        except (UpstreamError, ValueError) as exc:
            self.weather = {k: {**v, "stale": True} for k, v in self.weather.items()}
            self.weather_error = str(exc)
            log.warning("weather refresh failed: %s", exc)

    async def refresh_prices(self) -> None:
        if self.clients.prices is None:
            return
        now = datetime.now(UTC)
        today_missing = not any(s.start.date() <= now.date() for s in self._slots) if self._slots else True
        if not today_missing and not self._due_now("prices", PRICES_EVERY_S):
            self.prices = summarize(self._slots, now)
            return
        if today_missing and not self._due_now("prices-retry", PRICES_RETRY_S):
            return
        try:
            self._slots = await self.clients.prices.day_ahead(now)
            self.prices_error = None
            self._due["prices"] = time.monotonic() + PRICES_EVERY_S
        except PriceError as exc:
            self.prices_error = str(exc)
            log.warning("price refresh failed: %s", exc)
        self.prices = summarize(self._slots, now) if self._slots else {"available": False}

    # ---- the loop ---------------------------------------------------------------------
    async def poll_once(self) -> None:
        errors: dict[str, str] = {}
        stations: list[dict] = []
        proxies: dict = {}
        try:
            stations = await self.clients.csms.fleet()
        except UpstreamError as exc:
            errors["csms"] = str(exc)
        try:
            proxies = await self.clients.toxiproxy.proxies()
        except UpstreamError as exc:
            errors["toxiproxy"] = str(exc)
        await self.refresh_weather()
        await self.refresh_prices()
        if self.weather_error:
            errors["weather"] = self.weather_error
        if self.prices_error:
            errors["prices"] = self.prices_error

        snap = build_snapshot(self.sites, stations, proxies, self.weather, errors, _now_iso())
        now_mono = time.monotonic()
        dt = now_mono - self._last_poll if self._last_poll else 0.0
        self._last_poll = now_mono
        for site in snap["sites"]:
            site["solar_w"] = solar_w(site, site.get("weather"))
        if stations and dt:
            self.batteries.tick(snap["sites"], self.prices, min(dt, 30.0))
        batteries = self.batteries.status()
        for site in snap["sites"]:
            b = batteries.get(site["code"])
            site["battery"] = b
            # Grid draw = chargers - solar + battery (charging adds, discharging removes). That is what the cap limits.
            grid = max(0.0, site["power_w"] - (site.get("solar_w") or 0.0) + ((b or {}).get("power_w") or 0.0))
            site["grid_w"] = round(grid, 1)
            site["cap_used"] = round(grid / site["cap_w"], 4) if site.get("cap_w") else None
            site["battery_support_w"] = self.batteries.support_w(site["code"])
        if stations and dt:
            self.economics.accrue(snap["sites"], self.prices, min(dt, 30.0), datetime.now(UTC))
            self.uptime.accrue(snap["stations"], min(dt, 30.0), datetime.now(UTC))
        uptime = self.uptime.summary()
        snap["kpis"]["uptime_today"] = uptime["fleet"]
        snap["kpis"]["uptime_since"] = uptime["since"]
        money = self.economics.summary()
        demand = self.demand.status()
        shares, reasons = self.world.shares(), self.world.reasons()
        tariffs = self.economics.tariffs(self.prices)
        by_code = {s["code"]: s for s in self.sites}
        for site in snap["sites"]:
            code = site["code"]
            factor = demand_factor(by_code[code], tariffs[code])
            site["economics"] = money["sites"].get(code, {})
            site["uptime_today"] = uptime["sites"].get(code)
            site["downtime_min"] = uptime["connector_downtime_min"].get(code)
            site["profile"] = by_code[code].get("demand")  # "demand" below becomes live demand stats
            site["demand"] = {**demand.get(code, {}), "share": shares.get(code), "reason": reasons.get(code),
                              "price_factor": factor}
            site["rules"] = [r.__dict__ for r in self.world.rules if r.site == site["code"]]
            site["controller"] = self.controller.status().get(site["code"])
        sessions = self.demand.session_info({s["id"]: s for s in snap["stations"]})
        for st in snap["stations"]:
            st["limit_sources"] = {SOURCE_LABELS.get(k, k): v for k, v in self.limits.sources_for(st["id"]).items()}
            for c in st.get("connectors", []):
                c["session"] = sessions.get(f"{st['id']}#{c['id']}")

        if self.snapshot.get("stations"):
            for ev in group_events(detect_events(self.snapshot["stations"], snap["stations"])):
                self.add_event(ev["level"], ev["text"], ev["site"], ev["station"])
        if stations:
            await self._automatic(snap)

        snap["kpis"]["queue"] = sum(d["queue"] for d in demand.values())
        sun = next(iter(self.weather.values()), {}) if self.weather else {}
        self.snapshot = {
            **snap,
            "events": list(self.events),
            "incidents": self.incidents.snapshot(),
            "world": self.world.status(),
            "scenario": self.runner.status(),
            "autopilot": self.autopilot.status(),
            "prices": self.prices,
            "daylight": daylight(datetime.now(UTC), sun.get("sunrise"), sun.get("sunset")),
            "grafana_url": self.grafana_url,
            "economics": money,
            "batteries_enabled": self.batteries.enabled,
        }

    async def _automatic(self, snap: dict) -> None:
        """Everything that acts on its own: cleanup, incidents, controller, real-world mode, autopilot."""
        try:
            if not self._reconciled:
                self._reconciled = True
                cleared = await self.limits.reconcile(snap["stations"])
                for s in self.sites:
                    await self.clients.toxiproxy.set_enabled(s["proxy"], True)
                    await self.clients.toxiproxy.set_latency(s["proxy"], 0)
                if cleared:
                    self.add_event("info", f"startup: cleared {len(cleared)} leftover power limits")
            await self.limits.retry_pending({s["id"] for s in snap["stations"] if s.get("connected")})
            await self.incidents.observe(snap)
            if self._due_now("controller", CONTROLLER_EVERY_S):
                for ev in await self.controller.tick(snap):
                    self.incidents.grid_cap(ev)
            self._flex_requests()
            if self._due_now("demand", DEMAND_EVERY_S):
                tariffs = self.economics.tariffs(self.prices)
                factors = {s["code"]: demand_factor(s, tariffs[s["code"]]) for s in self.sites}
                for text in await self.demand.tick(snap, self.world.shares(), factors):
                    self.add_event("info", text, kind="demand")
            if self._due_now("world", WORLD_EVERY_S):
                await self.tick_world(snap)
            if self._due_now("save", SAVE_EVERY_S):
                self._save_state()
            started = self.autopilot.tick(snap["stations"])
            if started:
                self.add_event("info", f"autopilot picked a random scenario: {started}", kind="scenario")
        except UpstreamError as exc:
            self.add_event("error", f"automatic action failed: {exc}")

    def _flex_requests(self) -> None:
        requested = self.limits.stations_for("grid-operator")
        for site in self.sites:
            mine = [sid for sid in requested if sid.startswith(f"{site['code']}-")]
            self.incidents.flex_request(site["code"], bool(mine), len(mine),
                                        f"Congestion request: {site['code']} must cut to 50% of its grid cap.")

    async def tick_world(self, snap: dict) -> None:
        self._due["world"] = time.monotonic() + WORLD_EVERY_S
        try:
            for text in await self.world.tick(snap, self.weather, self.prices):
                self.add_event("info", f"real world: {text}", kind="world")
        except UpstreamError as exc:
            self.add_event("error", f"real-world mode: {exc}")

    async def run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001 - the poller must survive anything
                log.exception("poll failed")
            await asyncio.sleep(POLL_S)
