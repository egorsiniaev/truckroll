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
from control.decisions import DecisionBook
from control.demand import DemandEngine
from control.disruption import OutagePlanner
from control.economics import Economics, demand_factor
from control.fieldservice import FieldService
from control.firmware import FirmwareManager
from control.incidents import IncidentManager
from control.limits import SOURCE_LABELS, LimitArbiter
from control.persist import Clocks
from control.prices import PriceClient, PriceError, summarize
from control.scenarios import Ledger, Runner
from control.slo import SloTracker
from control.state import build_snapshot, detect_events, group_events
from control.uptime import UptimeTracker
from control.world import WorldEngine, solar_w

log = logging.getLogger(__name__)

POLL_S = 2.0
CONTROLLER_EVERY_S = 10
DEMAND_EVERY_S = 5
AGENT_EVERY_S = 5
WORLD_EVERY_S = 60
WEATHER_EVERY_S = 600
FEEDS_EVERY_S = 30  # weather and prices: each has its own cadence inside
PRICES_EVERY_S = 3600
PRICES_RETRY_S = 600
SAVE_EVERY_S = 60
FIRMWARE_EVERY_S = 10
FILINGS_EVERY_S = 60
DAILY_REPORT_AT = (23, 55)  # local: the day's NIS2/CRA report is written just before midnight
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
    hitl_default: bool = True
    agent: object | None = None  # TriageAgent, when a Gemini key is configured
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
    _fixes_applied: set = field(default_factory=set)

    def __post_init__(self) -> None:
        self.limits = LimitArbiter(self.clients.csms)
        self.world = WorldEngine(self.sites, self.clients, self.limits, enabled=self.world_default)
        self.runner = Runner(self.clients, self.sites, Ledger(self.ledger_path), self._scenario_event,
                             limits=self.limits, world=self.world)
        self.controller = SiteController(self.sites, self.limits)
        self.book = DecisionBook(enabled=self.hitl_default)
        self.incidents = IncidentManager(self.sites, self.clients, book=self.book, limits=self.limits)
        self.field = FieldService(self.clients, on_cost=lambda site, eur: self.economics.service(site, eur))
        self.incidents.field = self.field
        self.history_path = self.ledger_path.with_name("incidents.jsonl") if self.ledger_path else None
        from control.reports import Reports
        self.reports = Reports(self.ledger_path.with_name("reports") if self.ledger_path else None)
        self.incidents.on_closed = self._append_history
        self.slo = SloTracker([s["code"] for s in self.sites])
        self.autopilot = Autopilot(self.runner, enabled=self.autopilot_default)
        self.economics = Economics(self.sites, dynamic=self.dynamic_pricing_default)
        self.uptime = UptimeTracker([s["code"] for s in self.sites])
        self.batteries = BatteryFleet(self.sites, enabled=self.batteries_default)
        self.state_path = self.ledger_path.with_name("state.json") if self.ledger_path else None
        self._fw_events: list[dict] = []
        self.firmware = FirmwareManager(self.sites, self.clients,
                                        self.ledger_path.with_name("firmware.jsonl") if self.ledger_path else None,
                                        on_event=self._fw_events.append)
        self.demand = DemandEngine(self.sites, self.clients, self.id_tags,
                                   on_lost=lambda site, kwh, why: self.economics.lost(site, kwh, self.prices, why),
                                   blocked=lambda: set(self.firmware.installs))
        self.incidents.firmware = self.firmware
        from control.filings import Filings
        self.filings = Filings(self.ledger_path.with_name("filings.json") if self.ledger_path else None)
        self._filings_due: list[dict] = []
        self.outages = OutagePlanner([s["code"] for s in self.sites], self.runner, self._lost_today)
        self._load_state()  # last: needs every component above
        if self.history_path is not None and not self.history_path.exists():
            # First run with a history file: seed it with the recently closed incidents still in memory.
            for inc in reversed(self.incidents.closed):
                self._append_history(inc.to_json(self.incidents.clock()))

    def _lost_today(self, code: str) -> float:
        led = self.economics.today.get(code)
        return led.lost_eur if led else 0.0

    def _append_history(self, incident: dict) -> None:
        if self.history_path is None:
            return
        try:
            with self.history_path.open("a") as f:
                f.write(json.dumps(incident) + "\n")
        except OSError as exc:
            log.warning("incident history write failed: %s", exc)

    def _correct_restart_closures(self) -> None:
        """One-off: meter-jump incidents closed by a misread timer after the 13:29:31Z restart get true records."""
        bad_at = "2026-11-25T13:29:31Z"
        fixed: dict[str, dict] = {}
        for inc in self.incidents.closed:
            if inc.kind != "meter_jump" or inc.recovered != bad_at:
                continue
            inc.ttr_s = round((_parse_iso(bad_at) - _parse_iso(inc.opened)).total_seconds())
            last = inc.timeline[-1]
            if last["type"] == "recovered" and last["short"] != "closed by bug":
                last.update(text=f"Closed by a restart bug: a saved timer was misread, so the 30-minute quiet check "
                                 f"passed without being checked. Open for {inc.ttr_s // 60} min; recovery not "
                                 f"verified.", short="closed by bug")
            held = next((t["time"] for t in inc.timeline if t["short"] == "billing hold"), inc.opened)
            inc.memo.update(hold_since=held, hold_released=_iso_now())
            inc.timeline.insert(len(inc.timeline) - 1, {
                "time": inc.memo["hold_released"], "type": "response", "actor": "operator", "short": "billing released",
                "text": "Billing released at review: held sessions re-billed from their power readings."})
            fixed[inc.id] = inc.to_json(self.incidents.clock())
        if not fixed or self.history_path is None or not self.history_path.exists():
            return
        rows = [json.loads(line) for line in self.history_path.read_text().splitlines() if line.strip()]
        self.history_path.write_text("".join(json.dumps(fixed.get(r.get("id"), r)) + "\n" for r in rows))
        log.info("corrected %d incidents closed by the restart bug", len(fixed))

    def history(self) -> list[dict]:
        """Every closed incident on record, plus the ones still open."""
        rows = []
        if self.history_path and self.history_path.exists():
            for line in self.history_path.read_text().splitlines():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        live = [i for i in self.incidents.snapshot() if i["status"] != "recovered"]
        return live + rows[::-1]

    # ---- survive restarts: uptime, today's money, battery charge -------------------
    def _save_state(self) -> None:
        if not self.state_path or not self.uptime.day:
            return
        clocks = Clocks()
        data = {
            "day": self.uptime.day, "uptime_since": self.uptime.since,
            "uptime": {c: [v.up_s, v.total_s, v.up_x, v.total_x] for c, v in self.uptime.per_site.items()},
            "slo": self.slo.to_state(),
            "decisions": self.book.to_state(clocks),
            "field": self.field.to_state(clocks),
            "economics_day": self.economics.day,
            "economics": {c: led.__dict__ for c, led in self.economics.today.items()},
            "battery_soc_kwh": {c: b.soc_kwh for c, b in self.batteries.batteries.items()},
            "saved_wall": clocks.wall,
            "fixes": sorted(self._fixes_applied),
            "demand": self.demand.to_state(clocks),
            "incidents": self.incidents.to_state(clocks),
            "firmware": self.firmware.to_state(),
            "outages": self.outages.to_state(),
        }
        try:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(self.state_path)
            log.info("state saved")
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
            # Older saves have no "excluding injected" counters: start them from the experienced ones.
            self.uptime.per_site = {c: Counter(*(v if len(v) == 4 else [*v, *v])) for c, v in data.get("uptime", {}).items()}
        if data.get("slo", {}).get("day") == today:
            self.slo.from_state(data["slo"])
        if data.get("economics_day") == today:
            self.economics.day = today
            self.economics.today = {c: Ledger(**v) for c, v in data.get("economics", {}).items()}
        for code, soc in data.get("battery_soc_kwh", {}).items():
            if code in self.batteries.batteries:
                self.batteries.batteries[code].soc_kwh = soc
        clocks = Clocks()
        try:
            if "demand" in data:
                self.demand.from_state(data["demand"], clocks)
            if "incidents" in data:
                self.incidents.from_state(data["incidents"], clocks)
            if "decisions" in data:
                self.book.from_state(data["decisions"], clocks)
            if "field" in data:
                self.field.from_state(data["field"], clocks)
            if "firmware" in data:
                self.firmware.from_state(data["firmware"])
            if "outages" in data:
                self.outages.from_state(data["outages"])
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("could not restore queue/incidents, starting them fresh: %s", exc)
        self._fixes_applied = set(data.get("fixes", []))
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
        backend: dict = {}
        if stations:
            try:
                backend = await self.clients.csms.stats()
            except UpstreamError as exc:
                errors["csms-stats"] = str(exc)
        if self.weather_error:
            errors["weather"] = self.weather_error
        if self.prices_error:
            errors["prices"] = self.prices_error

        snap = build_snapshot(self.sites, stations, proxies, self.weather, errors, _now_iso())
        snap["backend"] = backend  # before the incident engine runs: its backend rules read it
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
            run = self.runner.status().get("running") or {}
            self.uptime.accrue(snap["stations"], min(dt, 30.0), datetime.now(UTC),
                               excluded_sites=set(run.get("sites", [])), excluded_stations=set(run.get("stations", [])))
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
        installing = {i.station: i for i in self.firmware.installs.values()}
        for st in snap["stations"]:
            st["firmware"] = self.firmware.versions.get(st["id"])
            if (job := installing.get(st["id"])) is not None:
                st["firmware_job"] = {"phase": job.phase, "to": job.source if job.phase == "rolling_back" else job.target}
            st["limit_sources"] = {SOURCE_LABELS.get(k, k): v for k, v in self.limits.sources_for(st["id"]).items()}
            for c in st.get("connectors", []):
                c["session"] = sessions.get(f"{st['id']}#{c['id']}")

        if self.snapshot.get("stations"):
            for ev in group_events(detect_events(self.snapshot["stations"], snap["stations"])):
                self.add_event(ev["level"], ev["text"], ev["site"], ev["station"])
        if stations:
            await self._automatic(snap)

        snap["kpis"]["queue"] = sum(d["queue"] for d in demand.values())
        if stations and dt:
            flex = {}
            for site in self.sites:
                mine = [w for sid, w in self.limits.stations_for("grid-operator").items() if sid.startswith(f"{site['code']}-")]
                if mine:
                    flex[site["code"]] = sum(mine)
            self.slo.accrue(snap["sites"], flex, min(dt, 30.0), time.monotonic(), self.uptime.day)
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
            "slo": self.slo.summary(uptime, demand) + self.slo.response_summary(self.incidents.snapshot(),
                                                                                 self.book.history),
            "field_jobs": [j.to_json() | {"in_s": max(0, round(j.done_at - time.monotonic()))}
                           for j in self.field.jobs.values() if j.status != "done"],
            "decisions": self.book.snapshot(),
            "firmware": self.firmware.status(),
            "area_outage": self.outages.status(),
            "filings_due": self._filings_due,
            "agent": self.agent.status() if self.agent is not None else None,
        }

    async def _automatic(self, snap: dict) -> None:
        """Everything that acts on its own: cleanup, incidents, controller, real-world mode, autopilot."""
        try:
            if "meter-baseline-2026-11-25" not in self._fixes_applied:
                # One-off: drift incidents opened before the stale-baseline fix were false alarms.
                self._fixes_applied.add("meter-baseline-2026-11-25")
                for key in [k for k in self.incidents.open if k.startswith("meter:")]:
                    self.incidents._close(key, "Closed as a false alarm: the drift check used the previous session's "
                                               "last reading as this session's start (detector bug, fixed). The meter "
                                               "was fine.", "system", short="false alarm")
            if "restart-close-2026-11-25b" not in self._fixes_applied:
                self._fixes_applied.add("restart-close-2026-11-25b")
                self._correct_restart_closures()
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
            await self._drain_firmware()
            await self.field.tick(self.incidents)
            if self._due_now("controller", CONTROLLER_EVERY_S):
                for ev in await self.controller.tick(snap):
                    self.incidents.grid_cap(ev)
            self._flex_requests()
            if self._due_now("world", WORLD_EVERY_S):
                await self.tick_world(snap)
            if self._due_now("save", SAVE_EVERY_S):
                self._save_state()
            outage = self.outages.tick(snap["stations"])  # before the autopilot: today's outage goes first
            if outage:
                self.add_event("error" if "power and line down" in outage else "info", outage, kind="scenario")
            self._daily_report_due()
            if self._due_now("filings", FILINGS_EVERY_S):
                from control.filings import due_soon
                self._filings_due = due_soon(self.filing_packs(), datetime.now(UTC))
            started = self.autopilot.tick(snap["stations"])
            if started:
                self.add_event("info", f"autopilot picked a random scenario: {started}", kind="scenario")
        except UpstreamError as exc:
            self.add_event("error", f"automatic action failed: {exc}")

    async def _drain_firmware(self) -> None:
        events, self._fw_events[:] = list(self._fw_events), []
        levels = {"install_failed": "error", "release_failing": "error", "rolled_back": "ok", "reverted": "ok",
                  "release_halted": "error"}
        for ev in events:
            if ev["type"] != "installed" and ev["type"] != "install_started":  # per-charger noise stays in the log
                self.add_event(levels.get(ev["type"], "info"), f"firmware: {ev['text']}", ev.get("site"),
                               ev.get("station"), kind="firmware")
            await self.incidents.firmware_event(ev)

    # ---- daily NIS2 / CRA report ---------------------------------------------------------
    def daily_report(self, day: str, preliminary: bool = True) -> dict:
        from control.compliance import daily
        today = self.economics.day == day
        return daily(day, self.history(), self.firmware.history(day), self.outages.for_day(day),
                     self.firmware.versions, [r.public() for r in self.firmware.releases],
                     self.economics.summary() if today else None,
                     self.uptime.summary()["fleet"] if self.uptime.day == day else None, _now_iso(), preliminary)

    def filing_packs(self) -> list[dict]:
        """One pack per NIS2 significant incident and per actively exploited vulnerability, newest first."""
        from control.filings import cra_pack, nis2_pack
        from control.firmware import parse
        from control.reports import applicability
        sites = {s["code"]: s for s in self.sites}
        outages = [*self.outages.log] + ([self.outages.for_day(self.outages.plan["day"])]
                                         if self.outages.plan.get("started") and not self.outages.plan.get("ended") else [])

        def loss(inc: dict) -> float | None:
            t = _parse_iso(inc["opened"])
            for o in outages:
                start = _parse_iso(o["started"])
                if o["site"] == inc["site"] and abs((t - start).total_seconds()) <= 180:
                    return o["loss_eur"]
            return None

        packs = [nis2_pack(i, sites, loss(i)) for i in self.history() if applicability(i)["nis2"]["applies"]]
        for r in self.firmware.release_log():
            if r.get("exploited"):
                need = parse(r["version"])
                on = {s for s, v in self.firmware.versions.items() if parse(v) >= need}
                packs.append(cra_pack(r, self.firmware.versions, on))
        return sorted((self.filings.decorate(p) for p in packs), key=lambda p: p["opened"], reverse=True)

    def _daily_report_due(self) -> None:
        from control.uptime import TZ
        local = datetime.now(UTC).astimezone(TZ)
        day = local.strftime("%Y-%m-%d")
        if (local.hour, local.minute) >= DAILY_REPORT_AT and self.reports.get_daily(day) is None:
            rep = self.daily_report(day, preliminary=False)
            self.reports.save_daily(rep)
            self.add_event("info", f"daily NIS2/CRA report for {day}: {rep['headline']}", kind="report")

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

    async def demand_once(self) -> None:
        snap = self.snapshot
        if not snap.get("stations"):
            return
        tariffs = self.economics.tariffs(self.prices)
        factors = {s["code"]: demand_factor(s, tariffs[s["code"]]) for s in self.sites}
        for text in await self.demand.tick(snap, self.world.shares(), factors):
            self.add_event("info", text, kind="demand")

    async def firmware_once(self) -> None:
        if self.snapshot.get("stations"):
            await self.firmware.tick(self.snapshot["stations"])

    async def agent_once(self) -> None:
        if self.agent is not None and self.snapshot.get("stations"):
            await self.agent.tick(self.incidents, {**self.snapshot, "events": list(self.events), "prices": self.prices})

    async def feeds_once(self) -> None:
        await self.refresh_weather()
        await self.refresh_prices()

    async def _loop(self, name: str, step, every_s: float) -> None:
        while True:
            try:
                await step()
            except Exception:  # noqa: BLE001 - a background loop must survive anything
                log.exception("%s loop failed", name)
            await asyncio.sleep(every_s)

    async def run(self) -> None:
        # Slow upstreams (simulator commands, Gemini) run in their own loops, so they can never freeze the
        # 2 s poll that keeps the map, detection and decisions current.
        side = [asyncio.create_task(self._loop("demand", self.demand_once, DEMAND_EVERY_S)),
                asyncio.create_task(self._loop("agent", self.agent_once, AGENT_EVERY_S)),
                asyncio.create_task(self._loop("feeds", self.feeds_once, FEEDS_EVERY_S)),
                asyncio.create_task(self._loop("firmware", self.firmware_once, FIRMWARE_EVERY_S))]
        try:
            await self._loop("poll", self.poll_once, POLL_S)
        finally:
            for t in side:
                t.cancel()


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))
