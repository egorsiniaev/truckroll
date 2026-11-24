"""HTTP API and web page. A single poller builds the snapshot; every page client streams it over SSE."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from control.clients import CsmsClient, SimulatorClient, ToxiproxyClient, UpstreamError, WeatherClient
from control.scenarios import Ledger, Runner
from control.state import build_snapshot, detect_events, group_events
from control.world import WorldEngine, solar_w

log = logging.getLogger(__name__)

POLL_S = 2.0
WORLD_EVERY_S = 60
WEATHER_EVERY_S = 600
STREAM_EVERY_S = 2.0
MAX_EVENTS = 60
ERROR_CODES = (
    "ConnectorLockFailure", "EVCommunicationError", "GroundFailure", "HighTemperature", "InternalError",
    "OverCurrentFailure", "OverVoltage", "PowerMeterFailure", "PowerSwitchFailure", "ReaderFailure",
    "UnderVoltage", "WeakSignal", "OtherError",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class Clients:
    csms: CsmsClient
    toxiproxy: ToxiproxyClient
    simulator: SimulatorClient
    weather: WeatherClient | None


@dataclass
class Hub:
    sites: list[dict]
    clients: Clients
    snapshot: dict = field(default_factory=dict)
    events: deque = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    weather: dict = field(default_factory=dict)
    weather_at: float = 0.0
    weather_error: str | None = None
    ledger_path: Path | None = None
    world: WorldEngine = field(init=False)
    runner: Runner = field(init=False)
    world_at: float = 0.0

    def __post_init__(self) -> None:
        self.world = WorldEngine(self.sites, self.clients)
        self.runner = Runner(self.clients, self.sites, Ledger(self.ledger_path), lambda lvl, txt: self.add_event(lvl, txt))

    def add_event(self, level: str, text: str, site: str | None = None, station: str | None = None) -> None:
        self.events.appendleft({"time": _now_iso(), "level": level, "text": text, "site": site, "station": station})

    async def refresh_weather(self) -> None:
        if self.clients.weather is None or time.monotonic() - self.weather_at < WEATHER_EVERY_S and self.weather:
            return
        try:
            rows = await self.clients.weather.current([(s["lat"], s["lon"]) for s in self.sites])
            self.weather = {s["code"]: {**row, "stale": False} for s, row in zip(self.sites, rows, strict=True)}
            self.weather_error = None
        except (UpstreamError, ValueError) as exc:
            # Keep the last reading and mark it stale rather than blanking the scene.
            self.weather = {k: {**v, "stale": True} for k, v in self.weather.items()}
            self.weather_error = str(exc)
            log.warning("weather refresh failed: %s", exc)
        self.weather_at = time.monotonic()

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
        if self.weather_error:
            errors["weather"] = self.weather_error
        snap = build_snapshot(self.sites, stations, proxies, self.weather, errors, _now_iso())
        for site in snap["sites"]:
            site["solar_w"] = solar_w(site, site.get("weather"))
        if self.snapshot.get("stations"):
            for ev in group_events(detect_events(self.snapshot["stations"], snap["stations"])):
                self.add_event(ev["level"], ev["text"], ev["site"], ev["station"])
        if stations and time.monotonic() - self.world_at >= WORLD_EVERY_S:
            await self.tick_world(snap)
        self.snapshot = {**snap, "events": list(self.events), "world": self.world.status(),
                         "scenario": self.runner.status()}

    async def tick_world(self, snap: dict) -> None:
        self.world_at = time.monotonic()
        try:
            for text in await self.world.tick(snap, self.weather):
                self.add_event("info", f"real world: {text}")
        except UpstreamError as exc:
            self.add_event("error", f"real-world mode: {exc}")

    async def run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001 - the poller must survive anything
                log.exception("poll failed")
            await asyncio.sleep(POLL_S)


class OutageRequest(BaseModel):
    on: bool


class LatencyRequest(BaseModel):
    ms: int = Field(ge=0, le=30_000)


class LimitRequest(BaseModel):
    watts: float | None = Field(default=None, ge=0, le=400_000)


class FaultRequest(BaseModel):
    error_code: Literal[ERROR_CODES] = "OtherError"  # type: ignore[valid-type]


class PowerRequest(BaseModel):
    on: bool


class WorldRequest(BaseModel):
    enabled: bool


def create_app(hub: Hub, web_dir: Path | None, start_poller: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(hub.run()) if start_poller else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="truckroll-control", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    sites_by_code = {s["code"]: s for s in hub.sites}

    def site_or_404(code: str) -> dict:
        if code not in sites_by_code:
            raise HTTPException(404, f"unknown site {code}")
        return sites_by_code[code]

    def station_or_404(station_id: str) -> dict:
        for s in hub.snapshot.get("stations", []):
            if s["id"] == station_id:
                return s
        raise HTTPException(404, f"unknown station {station_id}")

    async def act(coro, ok_text: str, site: str | None = None, station: str | None = None):
        try:
            result = await coro
        except UpstreamError as exc:
            hub.add_event("error", f"control failed: {exc}", site, station)
            raise HTTPException(502, str(exc)) from exc
        hub.add_event("info", ok_text, site, station)
        await hub.poll_once()
        return {"ok": True, "result": result}

    @app.get("/api/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/api/state")
    async def state():
        return hub.snapshot

    @app.get("/api/meta")
    async def meta():
        return {"sites": hub.sites, "error_codes": ERROR_CODES, "scenarios": hub.runner.catalog()}

    @app.post("/api/world")
    async def world(body: WorldRequest):
        async def apply():
            if body.enabled:
                hub.world.enabled = True
                await hub.tick_world(hub.snapshot)
            else:
                await hub.world.disable()

        text = "real-world mode on: local time and weather now drive the fleet" if body.enabled else "real-world mode off"
        return await act(apply(), text)

    @app.post("/api/scenarios/{scenario_id}/start")
    async def start_scenario(scenario_id: str):
        try:
            status = hub.runner.start(scenario_id, hub.snapshot.get("stations", []))
        except KeyError:
            raise HTTPException(404, f"unknown scenario {scenario_id}") from None
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None
        await hub.poll_once()
        return {"ok": True, "result": status}

    @app.post("/api/scenarios/stop")
    async def stop_scenario():
        await hub.runner.stop()
        await hub.poll_once()
        return {"ok": True, "result": hub.runner.status()}

    @app.get("/api/stream")
    async def stream(request: Request):
        async def events() -> AsyncIterator[str]:
            while not await request.is_disconnected():
                yield f"data: {json.dumps(hub.snapshot)}\n\n"
                await asyncio.sleep(STREAM_EVERY_S)

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    @app.post("/api/sites/{code}/outage")
    async def outage(code: str, body: OutageRequest):
        site = site_or_404(code)
        text = "network outage started" if body.on else "network restored"
        return await act(hub.clients.toxiproxy.set_enabled(site["proxy"], not body.on), text, site=code)

    @app.post("/api/sites/{code}/latency")
    async def latency(code: str, body: LatencyRequest):
        site = site_or_404(code)
        text = f"network latency set to {body.ms} ms" if body.ms else "network latency removed"
        return await act(hub.clients.toxiproxy.set_latency(site["proxy"], body.ms), text, site=code)

    @app.post("/api/chaos/clear")
    async def clear_chaos():
        async def clear_all():
            for s in hub.sites:
                await hub.clients.toxiproxy.set_enabled(s["proxy"], True)
                await hub.clients.toxiproxy.set_latency(s["proxy"], 0)

        return await act(clear_all(), "all network chaos cleared")

    @app.post("/api/stations/{station_id}/limit")
    async def limit(station_id: str, body: LimitRequest):
        st = station_or_404(station_id)
        text = f"power limit set to {body.watts / 1000:.1f} kW" if body.watts is not None else "power limit cleared"
        return await act(hub.clients.csms.set_limit(station_id, body.watts), text, st["site"], station_id)

    @app.post("/api/stations/{station_id}/fault")
    async def fault(station_id: str, body: FaultRequest):
        st = station_or_404(station_id)

        async def inject():
            for c in st["connectors"]:
                await hub.clients.simulator.set_status(station_id, c["id"], "Faulted", body.error_code)

        return await act(inject(), f"fault injected ({body.error_code})", st["site"], station_id)

    @app.post("/api/stations/{station_id}/clear-fault")
    async def clear_fault(station_id: str):
        st = station_or_404(station_id)

        async def clear():
            for c in st["connectors"]:
                await hub.clients.simulator.set_status(station_id, c["id"], "Available", "NoError")

        return await act(clear(), "fault cleared by operator", st["site"], station_id)

    @app.post("/api/stations/{station_id}/power")
    async def power(station_id: str, body: PowerRequest):
        st = station_or_404(station_id)
        text = "station switched on" if body.on else "station switched off"
        return await act(hub.clients.simulator.set_running(station_id, body.on), text, st["site"], station_id)

    @app.post("/api/stations/{station_id}/reset")
    async def reset(station_id: str):
        st = station_or_404(station_id)
        return await act(hub.clients.csms.reset(station_id), "soft reset sent", st["site"], station_id)

    if web_dir is not None and web_dir.is_dir():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app
