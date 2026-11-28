"""Scripted incidents with a known root cause. Each run is written to the incident ledger (ground truth)."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Step:
    at_s: float
    text: str
    action: Callable[[Ctx], Awaitable[None]]


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    description: str
    root_cause: str
    duration_s: float
    plan: Callable[[Ctx], list[Step]]
    cleanup: Callable[[Ctx], Awaitable[None]]
    icon: str = "⚠️"
    sites: tuple[str, ...] = ()  # where it happens; picked stations are added at run time
    scheduled: bool = False  # run by a planner, not offered on the panel or picked by the autopilot


@dataclass
class Ctx:
    """What a scenario may touch: the clients, the site table, the current stations, and a seeded random."""

    clients: object
    limits: object
    world: object
    sites: dict[str, dict]
    stations: list[dict]
    rng: random.Random
    picked: dict = field(default_factory=dict)

    def proxy(self, code: str) -> str:
        return self.sites[code]["proxy"]

    def stations_at(self, code: str) -> list[dict]:
        return sorted((s for s in self.stations if s["site"] == code and s.get("connected")), key=lambda s: s["id"])

    def pick(self, key: str, code: str, n: int = 1) -> list[dict]:
        if key not in self.picked:
            pool = self.stations_at(code)
            if not pool:
                raise RuntimeError(f"no online stations at {code}")
            self.picked[key] = self.rng.sample(pool, min(n, len(pool)))
        return self.picked[key]


def _outage(code: str, on: bool):
    async def run(c: Ctx) -> None:
        await c.clients.toxiproxy.set_enabled(c.proxy(code), not on)
    return run


def _latency(code: str, ms: int):
    async def run(c: Ctx) -> None:
        await c.clients.toxiproxy.set_latency(c.proxy(code), ms)
    return run


def _clear_sites(*codes: str):
    async def run(c: Ctx) -> None:
        for code in codes:
            await c.clients.toxiproxy.set_enabled(c.proxy(code), True)
            await c.clients.toxiproxy.set_latency(c.proxy(code), 0)
    return run


# --- storm at the highway hub -------------------------------------------------
def _storm(c: Ctx) -> list[Step]:
    return [
        Step(0, "gusts degrade the site uplink: +1500 ms latency", _latency("A2-HUB", 1500)),
        Step(30, "uplink drops", _outage("A2-HUB", True)),
        Step(55, "uplink back", _outage("A2-HUB", False)),
        Step(90, "uplink drops again", _outage("A2-HUB", True)),
        Step(105, "uplink back", _outage("A2-HUB", False)),
        Step(150, "storm passes, latency normal", _latency("A2-HUB", 0)),
    ]


# --- one overheating charger --------------------------------------------------
def _overheat(c: Ctx) -> list[Step]:
    async def fault(cx: Ctx) -> None:
        st = cx.pick("hot", "A2-HUB")[0]
        for conn in st["connectors"]:
            await cx.clients.simulator.set_status(st["id"], conn["id"], "Faulted", "HighTemperature")

    async def refault(cx: Ctx) -> None:
        # The heat problem is physical: after the remote reset it comes back.
        await fault(cx)

    async def clear(cx: Ctx) -> None:
        await _clear_faults(cx)

    return [Step(0, "power module overheats on one charger", fault),
            Step(80, "still too hot: the fault returns after the reset", refault),
            Step(240, "technician replaces the fan on site", clear)]


# --- backend and metering faults (injected in the CSMS, detected only from what it reports) -------------
def _csms(**settings):
    async def run(c: Ctx) -> None:
        await c.clients.csms.chaos(**settings)
    return run


def _charging_at(c: Ctx, code: str) -> list[dict]:
    busy = [s for s in c.stations_at(code) if any(conn.get("transaction_id") for conn in s.get("connectors", []))]
    return busy or c.stations_at(code)


def _auth_slow(c: Ctx) -> list[Step]:
    return [Step(0, "the RFID auth service starts answering in 35 s", _csms(auth_delay_s=35)),
            Step(330, "auth service recovers", _csms(auth_delay_s=0))]


async def _auth_cleanup(c: Ctx) -> None:
    await c.clients.csms.chaos(auth_delay_s=0, free_vend=False)


def _meter_drift(c: Ctx) -> list[Step]:
    async def drift(cx: Ctx) -> None:
        st = cx.picked.setdefault("meter", [cx.rng.choice(_charging_at(cx, "A2-HUB"))])[0]
        await cx.clients.csms.chaos(meter_factor={st["id"]: 1.8})

    return [Step(0, "one charger's energy meter starts reading 1.8x the real energy", drift),
            Step(480, "meter replaced", _csms(meter_factor={}))]


async def _meter_cleanup(c: Ctx) -> None:
    await c.clients.csms.chaos(meter_factor={})


def _ignores_limit(c: Ctx) -> list[Step]:
    async def ignore(cx: Ctx) -> None:
        st = cx.picked.setdefault("deaf", [cx.rng.choice(_charging_at(cx, "A2-HUB"))])[0]
        await cx.clients.csms.chaos(drop_profiles=[st["id"]])
        # A tight limit, so ignoring it is visible.
        await cx.limits.set("operator", st["id"], 20_000)

    return [Step(0, "a charger accepts power limits but never applies them; a 20 kW limit is set", ignore)]


async def _ignores_cleanup(c: Ctx) -> None:
    await c.clients.csms.chaos(drop_profiles=[])
    for st in c.picked.get("deaf", []):
        await c.limits.set("operator", st["id"], None)
        await c.clients.csms.availability(st["id"], "Operative")


def _backlog(c: Ctx) -> list[Step]:
    return [Step(0, "the backend starts answering every message in 20 s", _csms(response_delay_s=20)),
            Step(240, "load drops; replies fast again", _csms(response_delay_s=0))]


async def _backlog_cleanup(c: Ctx) -> None:
    await c.clients.csms.chaos(response_delay_s=0)


def _cold_snap(c: Ctx) -> list[Step]:
    async def cold(cx: Ctx) -> None:
        cx.world.override_temp["A2-HUB"] = -6.0

    return [Step(0, "a cold snap: -6 °C at the highway hub", cold)]


async def _cold_cleanup(c: Ctx) -> None:
    c.world.override_temp.pop("A2-HUB", None)


def _stuck(c: Ctx) -> list[Step]:
    async def stick(cx: Ctx) -> None:
        free = [s for s in cx.stations_at("EHV-OFFICE")
                if any(conn.get("status") == "Available" for conn in s.get("connectors", []))]
        pool = free or cx.stations_at("EHV-OFFICE")
        st = cx.picked.setdefault("stuck", [cx.rng.choice(pool)])[0]
        await cx.clients.simulator.set_status(st["id"], st["connectors"][0]["id"], "Preparing", "NoError")

    return [Step(0, "a connector gets stuck in Preparing: the cable lock handshake never completes", stick)]


def _lock_failure(c: Ctx) -> list[Step]:
    async def fault(cx: Ctx) -> None:
        st = cx.pick("lock", "UTR-RETAIL")[0]
        await cx.clients.simulator.set_status(st["id"], st["connectors"][0]["id"], "Faulted", "ConnectorLockFailure")

    return [Step(0, "connector lock jams on one retail charger", fault)]


async def _clear_faults(c: Ctx) -> None:
    for st in [s for group in c.picked.values() for s in group]:
        for conn in st["connectors"]:
            await c.clients.simulator.set_status(st["id"], conn["id"], "Available", "NoError")


# --- whole site offline -------------------------------------------------------
def _site_drop(c: Ctx) -> list[Step]:
    return [Step(0, "depot router loses its uplink", _outage("AMS-DEPOT", True)),
            Step(150, "ISP restores the line", _outage("AMS-DEPOT", False))]


# --- crash loop after a bad firmware update ------------------------------------
def _crash_loop(c: Ctx) -> list[Step]:
    steps: list[Step] = []
    for k in range(6):
        async def down(cx: Ctx) -> None:
            for st in cx.pick("loop", "UTR-RETAIL", 3):
                await cx.clients.simulator.set_running(st["id"], False)

        async def up(cx: Ctx) -> None:
            for st in cx.pick("loop", "UTR-RETAIL", 3):
                await cx.clients.simulator.set_running(st["id"], True)

        steps += [Step(k * 30, "3 updated chargers crash", down), Step(k * 30 + 12, "they reboot", up)]
    return steps


async def _restart_picked(c: Ctx) -> None:
    for st in c.picked.get("loop", []):
        await c.clients.simulator.set_running(st["id"], True)


# --- grid operator congestion request ------------------------------------------
def _congestion(c: Ctx) -> list[Step]:
    async def cut(cx: Ctx) -> None:
        stations = cx.stations_at("AMS-DEPOT")
        per_station = cx.sites["AMS-DEPOT"]["cap_w"] * 0.5 / max(1, len(stations))
        cx.picked["limited"] = stations
        await cx.limits.set_many("grid-operator", {st["id"]: per_station for st in stations})

    async def release(cx: Ctx) -> None:
        await _clear_limits(cx)

    return [Step(0, "grid operator: cut AMS-DEPOT to 50% of its cap", cut), Step(180, "congestion over, limits lifted", release)]


async def _clear_limits(c: Ctx) -> None:
    await c.limits.clear_source("grid-operator")


# --- everything reconnects at once ----------------------------------------------
def _reconnect_storm(c: Ctx) -> list[Step]:
    codes = list(c.sites)

    async def all_down(cx: Ctx) -> None:
        for code in codes:
            await cx.clients.toxiproxy.set_enabled(cx.proxy(code), False)

    async def all_up(cx: Ctx) -> None:
        for code in codes:
            await cx.clients.toxiproxy.set_enabled(cx.proxy(code), True)

    return [Step(0, "core network blip: every site loses the backend", all_down),
            Step(20, "network back: 50 stations reconnect at once", all_up)]


AREA_OUTAGE_S = 1800
ALL_SITES = ("AMS-DEPOT", "UTR-RETAIL", "A2-HUB", "EHV-OFFICE")


def _area_outage(code: str):
    def plan(c: Ctx) -> list[Step]:
        return [Step(0, f"grid fault in the area: {code} loses power and its line", _outage(code, True)),
                Step(AREA_OUTAGE_S, "grid operator restores the area", _outage(code, False))]
    return plan


def area_outage_id(code: str) -> str:
    return f"area-outage-{code.lower()}"


async def _noop(c: Ctx) -> None:
    return None


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("storm-a2", "Storm at A2-HUB", "Strong gusts make the hub's uplink flap for a couple of minutes.",
             "network: weather-related uplink flapping at A2-HUB", 160, _storm, _clear_sites("A2-HUB"), "⛈️", ("A2-HUB",)),
    Scenario("overheat", "Overheating charger", "One fast charger at A2-HUB reports HighTemperature and stops.",
             "hardware: power module overtemperature on a single charger", 250, _overheat, _clear_faults, "🌡️", ("A2-HUB",)),
    Scenario("lock-failure", "Jammed connector lock", "A connector lock jams at UTR-RETAIL; a remote reset frees it.",
             "hardware: connector lock failure, fixed by remote reset", 120, _lock_failure, _clear_faults, "🔒", ("UTR-RETAIL",)),
    Scenario("stuck-connector", "Connector stuck in Preparing", "A connector at EHV-OFFICE hangs in Preparing; "
             "nobody can charge there until the system notices.", "firmware: connector stuck in Preparing, cleared by "
             "status request or reset", 420, _stuck, _clear_faults, "⏳", ("EHV-OFFICE",)),
    Scenario("auth-slow", "RFID auth backend slow", "Card checks take 35 s, so sessions fail to start everywhere.",
             "backend: RFID auth service slow; sessions failed to start", 360, _auth_slow, _auth_cleanup, "🔑",
             ("AMS-DEPOT", "UTR-RETAIL", "A2-HUB", "EHV-OFFICE")),
    Scenario("meter-drift", "Meter drift", "One hub charger's energy meter reads 1.8x the real energy: wrong bills.",
             "hardware: energy meter drifting on one charger", 500, _meter_drift, _meter_cleanup, "📏", ("A2-HUB",)),
    Scenario("ignores-limit", "Charger ignores its power limit", "A hub charger accepts a 20 kW limit but keeps "
             "drawing full power.", "firmware: charger accepts power limits but does not apply them", 300,
             _ignores_limit, _ignores_cleanup, "🚫", ("A2-HUB",)),
    Scenario("backlog", "Backend overloaded", "Every backend reply takes 20 s; chargers stay connected but go quiet.",
             "backend: CSMS overloaded, replies delayed", 260, _backlog, _backlog_cleanup, "🐌",
             ("AMS-DEPOT", "UTR-RETAIL", "A2-HUB", "EHV-OFFICE")),
    Scenario("cold-snap", "Cold snap (expected)", "-6 °C at the hub: DC charging slows by design. Not a fault.",
             "expected: cold weather derate (not a fault)", 240, _cold_snap, _cold_cleanup, "🥶", ("A2-HUB",)),
    Scenario("site-drop", "Depot offline", "The depot's internet line drops; all 20 chargers go dark.",
             "network: site uplink outage at AMS-DEPOT", 160, _site_drop, _clear_sites("AMS-DEPOT"), "📡", ("AMS-DEPOT",)),
    Scenario("crash-loop", "Firmware crash loop", "Three chargers at UTR-RETAIL got a bad update and keep rebooting.",
             "firmware: crash loop after update on 3 chargers", 190, _crash_loop, _restart_picked, "🔁", ("UTR-RETAIL",)),
    Scenario("congestion", "Grid congestion request", "The grid operator asks the depot to halve its load for 3 minutes.",
             "expected: grid operator congestion request (not a fault)", 190, _congestion, _clear_limits, "⚡", ("AMS-DEPOT",)),
    Scenario("reconnect-storm", "Reconnect storm", "A 20-second core network blip; every station reconnects at once.",
             "network: core blip followed by mass reconnect", 60, _reconnect_storm,
             _clear_sites(*("AMS-DEPOT", "UTR-RETAIL", "A2-HUB", "EHV-OFFICE")), "🌐",
             ("AMS-DEPOT", "UTR-RETAIL", "A2-HUB", "EHV-OFFICE")),
    *(Scenario(area_outage_id(code), f"Area outage at {code}", f"A grid fault takes out {code} for 30 minutes.",
               f"network: area-wide power and line outage at {code}", AREA_OUTAGE_S + 30, _area_outage(code),
               _clear_sites(code), "🏙️", (code,), scheduled=True) for code in ALL_SITES),
)


class Ledger:
    """Append-only JSONL of scenario runs. This is the eval's ground truth; the triage agent must never read it."""

    def __init__(self, path: Path | None) -> None:
        self.path = path

    def write(self, record: dict) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as exc:
            log.error("ledger write failed: %s", exc)


def _iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class Runner:
    """Runs one scenario at a time. time_scale < 1 speeds runs up for tests."""

    def __init__(self, clients, sites: list[dict], ledger: Ledger, on_event: Callable[[str, str], None],
                 time_scale: float = 1.0, seed: int | None = None, limits=None, world=None) -> None:
        self.clients = clients
        self.limits = limits
        self.world = world
        self.sites = {s["code"]: s for s in sites}
        self.ledger = ledger
        self.on_event = on_event
        self.time_scale = time_scale
        self.rng = random.Random(seed)
        self.task: asyncio.Task | None = None
        self.current: Scenario | None = None
        self.ctx: Ctx | None = None
        self.started_at = 0.0
        self.started_iso = ""
        self.done_steps: list[str] = []
        self.run_id = ""

    def catalog(self) -> list[dict]:
        return [{"id": s.id, "title": s.title, "description": s.description, "duration_s": s.duration_s,
                 "icon": s.icon} for s in SCENARIOS if not s.scheduled]

    def status(self) -> dict:
        if not self.current:
            return {"running": None}
        stations = sorted({st["id"] for group in self.ctx.picked.values() for st in group}) if self.ctx else []
        return {"running": {"id": self.current.id, "title": self.current.title, "run_id": self.run_id,
                            "icon": self.current.icon, "sites": list(self.current.sites), "stations": stations,
                            "elapsed_s": round(time.monotonic() - self.started_at, 1),
                            "duration_s": self.current.duration_s, "steps_done": self.done_steps[-5:]}}

    def start(self, scenario_id: str, stations: list[dict]) -> dict:
        if self.task and not self.task.done():
            raise RuntimeError(f"scenario {self.current.id} is already running")
        scenario = next((s for s in SCENARIOS if s.id == scenario_id), None)
        if scenario is None:
            raise KeyError(scenario_id)
        self.current = scenario
        self.ctx = Ctx(self.clients, self.limits, self.world, self.sites, stations, self.rng)
        self.started_at = time.monotonic()
        self.started_iso = _iso()
        self.done_steps = []
        self.run_id = f"{scenario.id}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
        self.task = asyncio.create_task(self._run(scenario))
        return self.status()

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.current is not None:
            # Cancelled before its first step ran, so _run's cleanup never executed; nothing was changed yet.
            self.on_event("info", f"scenario cancelled before start: {self.current.title}")
            self.current = None

    async def _run(self, scenario: Scenario) -> None:
        ctx = self.ctx
        outcome = "completed"
        self.on_event("info", f"scenario started: {scenario.title}")
        try:
            steps = sorted(scenario.plan(ctx), key=lambda s: s.at_s)
            for step in steps:
                delay = step.at_s * self.time_scale - (time.monotonic() - self.started_at)
                if delay > 0:
                    await asyncio.sleep(delay)
                await step.action(ctx)
                self.done_steps.append(step.text)
                self.on_event("info", f"{scenario.title}: {step.text}")
            remaining = scenario.duration_s * self.time_scale - (time.monotonic() - self.started_at)
            if remaining > 0:
                await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            outcome = "stopped"
        except Exception as exc:  # noqa: BLE001 - a broken step must still clean up
            outcome = f"failed: {exc}"
            log.exception("scenario %s failed", scenario.id)
        finally:
            try:
                await scenario.cleanup(ctx)
            except Exception as exc:  # noqa: BLE001
                log.exception("cleanup for %s failed", scenario.id)
                outcome += f"; cleanup failed: {exc}"
            self.ledger.write({
                "run_id": self.run_id, "scenario": scenario.id, "root_cause": scenario.root_cause,
                "sites": list(scenario.sites),
                "started": self.started_iso, "ended": _iso(), "outcome": outcome,
                "targets": {k: [s["id"] for s in v] for k, v in ctx.picked.items()},
            })
            self.on_event("ok" if outcome == "completed" else "error", f"scenario {outcome}: {scenario.title}")
            self.current = None
