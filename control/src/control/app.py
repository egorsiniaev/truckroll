"""HTTP API and web page. The hub builds the snapshot; every page client streams it over SSE."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from control.clients import UpstreamError
from control.guard import Guard, client_ip
from control.hub import Clients, Hub  # noqa: F401 - re-exported for main and tests
from control.metrics import metrics_text

log = logging.getLogger(__name__)

STREAM_EVERY_S = 2.0
ERROR_CODES = (
    "ConnectorLockFailure", "EVCommunicationError", "GroundFailure", "HighTemperature", "InternalError",
    "OverCurrentFailure", "OverVoltage", "PowerMeterFailure", "PowerSwitchFailure", "ReaderFailure",
    "UnderVoltage", "WeakSignal", "OtherError",
)


class OutageRequest(BaseModel):
    on: bool


class LatencyRequest(BaseModel):
    ms: int = Field(ge=0, le=30_000)


class LimitRequest(BaseModel):
    watts: float | None = Field(default=None, ge=0, le=400_000)


class FaultRequest(BaseModel):
    error_code: Literal[ERROR_CODES] = "OtherError"  # type: ignore[valid-type]


class DecisionRequest(BaseModel):
    choice: Literal["approve", "reject"]


class ToggleRequest(BaseModel):
    on: bool | None = None
    enabled: bool | None = None

    @property
    def value(self) -> bool:
        return bool(self.enabled if self.enabled is not None else self.on)


def create_app(hub: Hub, web_dir: Path | None, start_poller: bool = True, guard: Guard | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(hub.run()) if start_poller else None
        yield
        if task:
            task.cancel()
        hub._save_state()  # on shutdown too, so a restart loses nothing since the last periodic save

    app = FastAPI(title="truckroll-control", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    sites_by_code = {s["code"]: s for s in hub.sites}
    guard = guard or Guard()

    @app.middleware("http")
    async def rate_limit(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path != "/api/healthz" and not guard.allow_request(request.method, client_ip(request)):
            return JSONResponse({"detail": "too many requests, try again in a minute"}, status_code=429,
                                headers={"Retry-After": str(int(guard.limits.window_s))})
        return await call_next(request)

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
            hub.add_event("error", f"operator action failed: {exc}", site, station, kind="operator")
            raise HTTPException(502, str(exc)) from exc
        hub.add_event("info", ok_text, site, station, kind="operator")
        await hub.poll_once()
        return {"ok": True, "result": result}

    @app.get("/api/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/api/state")
    async def state():
        return hub.snapshot

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics():
        return metrics_text(hub.snapshot)

    @app.post("/api/decisions/{decision_id}")
    async def decide(decision_id: str, body: DecisionRequest):
        try:
            d = hub.book.answer(decision_id, body.choice)
        except KeyError:
            raise HTTPException(404, f"no pending decision {decision_id}") from None
        hub.add_event("info", f"operator {'approved' if body.choice == 'approve' else 'rejected'}: {d.question}",
                      d.site, (d.stations or [None])[0], kind="operator")
        await hub.poll_once()  # act on it now, not on the next tick
        return {"ok": True, "result": d.to_json(hub.book.clock())}

    def _incident(incident_id: str) -> dict:
        inc = next((i for i in hub.history() if i["id"] == incident_id), None)
        if inc is None:
            raise HTTPException(404, f"unknown incident {incident_id}")
        return inc

    @app.get("/api/reports")
    async def reports_list():
        from control.reports import applicability
        rows = []
        for inc in hub.history()[:200]:
            rep = hub.reports.get(inc["id"])
            rows.append({"id": inc["id"], "title": inc["title"], "kind": inc["kind"], "site": inc["site"],
                         "opened": inc["opened"], "status": inc["status"], "ttr_s": inc.get("ttr_s"),
                         "applicability": applicability(inc), "has_report": rep is not None,
                         "approved": (rep or {}).get("approved", {})})
        return {"incidents": rows}

    @app.get("/api/reports/{incident_id}")
    async def report_get(incident_id: str):
        from control.reports import applicability
        inc = _incident(incident_id)
        return {"incident": inc, "applicability": applicability(inc), "report": hub.reports.get(incident_id)}

    @app.post("/api/reports/{incident_id}/draft")
    async def report_draft(incident_id: str):
        if hub.agent is None:
            raise HTTPException(409, "no Gemini key configured")
        try:
            return await hub.reports.draft(hub.agent, _incident(incident_id))
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from None

    @app.post("/api/reports/{incident_id}/approve/{part}")
    async def report_approve(incident_id: str, part: Literal["postmortem", "nis2_early_warning", "nis2_notification",
                                                            "cra_early_warning"]):
        try:
            return hub.reports.approve(incident_id, part)
        except KeyError:
            raise HTTPException(404, "draft the report first") from None

    @app.get("/api/summary")
    async def summary(since: str):
        from datetime import UTC, datetime

        from control.agent import summarize
        from control.summary import handover
        try:
            start = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(422, "since must be an ISO timestamp") from None
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        # Decisions are only kept briefly in memory; the incident timelines carry the rest.
        facts = handover(start, {**hub.snapshot, "incidents": hub.history()}, list(hub.book.history), hub.ledger_path)
        bullets = await summarize(hub.agent, facts) if hub.agent is not None and hub.agent.enabled else None
        return {"facts": facts, "ai": bullets}

    @app.post("/api/agent")
    async def agent_toggle(body: ToggleRequest):
        if hub.agent is None:
            raise HTTPException(409, "no Gemini key configured")
        hub.agent.enabled = body.value
        hub.add_event("info", "triage agent on" if body.value else "triage agent off", kind="operator")
        await hub.poll_once()
        return {"ok": True, "result": hub.agent.status()}

    @app.get("/api/eval")
    async def evaluation():
        from control.eval import evaluate
        if hub.ledger_path is None:
            raise HTTPException(409, "no ledger configured")
        return evaluate(hub.ledger_path, hub.ledger_path.with_name("triage.jsonl"))

    @app.post("/api/hitl")
    async def hitl(body: ToggleRequest):
        hub.book.enabled = body.value
        hub.add_event("info", "human approval on: risky actions wait for an operator" if body.value
                      else "human approval off: every response runs automatically", kind="operator")
        await hub.poll_once()
        return {"ok": True, "result": {"enabled": hub.book.enabled}}

    @app.post("/api/batteries")
    async def batteries(body: ToggleRequest):
        hub.batteries.enabled = body.value
        hub.add_event("info", "site batteries on" if body.value else "site batteries off", kind="operator")
        await hub.poll_once()
        return {"ok": True, "result": {"enabled": hub.batteries.enabled}}

    @app.post("/api/pricing")
    async def pricing(body: ToggleRequest):
        hub.economics.dynamic = body.value
        hub.add_event("info", "dynamic pricing on: price spikes reach public tariffs" if body.value
                      else "dynamic pricing off: fixed tariffs", kind="operator")
        await hub.poll_once()
        return {"ok": True, "result": {"dynamic": hub.economics.dynamic}}

    @app.get("/api/meta")
    async def meta():
        return {"sites": hub.sites, "error_codes": ERROR_CODES, "scenarios": hub.runner.catalog(),
                "grafana_url": hub.grafana_url}

    @app.get("/api/stream")
    async def stream(request: Request):
        ip = client_ip(request)
        if not guard.open_stream(ip):
            raise HTTPException(429, "too many open live views")

        async def events() -> AsyncIterator[str]:
            try:
                while not await request.is_disconnected():
                    yield f"data: {json.dumps(hub.snapshot)}\n\n"
                    await asyncio.sleep(STREAM_EVERY_S)
            finally:
                guard.close_stream(ip)

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    @app.post("/api/sites/{code}/outage")
    async def outage(code: str, body: OutageRequest):
        site = site_or_404(code)
        text = "operator started a network outage" if body.on else "operator restored the network"
        return await act(hub.clients.toxiproxy.set_enabled(site["proxy"], not body.on), text, site=code)

    @app.post("/api/sites/{code}/latency")
    async def latency(code: str, body: LatencyRequest):
        site = site_or_404(code)
        text = f"operator set network latency to {body.ms} ms" if body.ms else "operator removed network latency"
        return await act(hub.clients.toxiproxy.set_latency(site["proxy"], body.ms), text, site=code)

    @app.post("/api/chaos/clear")
    async def clear_chaos():
        async def clear_all():
            for s in hub.sites:
                await hub.clients.toxiproxy.set_enabled(s["proxy"], True)
                await hub.clients.toxiproxy.set_latency(s["proxy"], 0)

        return await act(clear_all(), "operator cleared all network chaos")

    @app.post("/api/stations/{station_id}/limit")
    async def limit(station_id: str, body: LimitRequest):
        st = station_or_404(station_id)
        text = f"operator limit {body.watts / 1000:.1f} kW" if body.watts is not None else "operator limit cleared"
        return await act(hub.limits.set("operator", station_id, body.watts), text, st["site"], station_id)

    @app.post("/api/stations/{station_id}/fault")
    async def fault(station_id: str, body: FaultRequest):
        st = station_or_404(station_id)

        async def inject():
            for c in st["connectors"]:
                await hub.clients.simulator.set_status(station_id, c["id"], "Faulted", body.error_code)

        return await act(inject(), f"operator injected a fault ({body.error_code})", st["site"], station_id)

    @app.post("/api/stations/{station_id}/clear-fault")
    async def clear_fault(station_id: str):
        st = station_or_404(station_id)

        async def clear():
            for c in st["connectors"]:
                await hub.clients.simulator.set_status(station_id, c["id"], "Available", "NoError")

        return await act(clear(), "operator cleared the fault", st["site"], station_id)

    @app.post("/api/stations/{station_id}/power")
    async def power(station_id: str, body: ToggleRequest):
        st = station_or_404(station_id)
        text = "operator switched the station on" if body.value else "operator switched the station off"
        return await act(hub.clients.simulator.set_running(station_id, body.value), text, st["site"], station_id)

    @app.post("/api/stations/{station_id}/reset")
    async def reset(station_id: str):
        st = station_or_404(station_id)
        return await act(hub.clients.csms.reset(station_id), "operator sent a soft reset", st["site"], station_id)

    @app.post("/api/world")
    async def world(body: ToggleRequest):
        async def apply():
            if body.value:
                hub.world.enabled = True
                await hub.tick_world(hub.snapshot)
            else:
                await hub.world.disable()

        return await act(apply(), "real-world mode on" if body.value else "real-world mode off")

    @app.post("/api/autopilot")
    async def autopilot(body: ToggleRequest):
        hub.autopilot.set_enabled(body.value)
        hub.add_event("info", "random scenarios on" if body.value else "random scenarios off", kind="operator")
        await hub.poll_once()
        return {"ok": True, "result": hub.autopilot.status()}

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

    if web_dir is not None and web_dir.is_dir():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app
