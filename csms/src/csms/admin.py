"""Internal admin API: fleet state as JSON and commands to stations. Never expose it publicly."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from fastapi import FastAPI, HTTPException
from ocpp.v16 import call
from ocpp.v16.enums import ChargingProfileKindType, ChargingProfilePurposeType, ChargingRateUnitType
from pydantic import BaseModel, Field

from csms.chaos import Chaos
from csms.fleet import FleetStore, Station
from csms.registry import Registry

log = logging.getLogger(__name__)

COMMAND_TIMEOUT_S = 15
LIMIT_PROFILE_ID = 9001  # one well-known id, so a new limit replaces the old one


class LimitRequest(BaseModel):
    watts: float | None = Field(default=None, ge=0, le=400_000, description="null clears the limit")


class ResetRequest(BaseModel):
    type: Literal["Soft", "Hard"] = "Soft"


class ChaosRequest(BaseModel):
    auth_delay_s: float | None = Field(default=None, ge=0, le=120)
    response_delay_s: float | None = Field(default=None, ge=0, le=120)
    meter_factor: dict[str, float] | None = None  # replaces the map; {} clears
    drop_profiles: list[str] | None = None  # replaces the set; [] clears
    free_vend: bool | None = None


class AvailabilityRequest(BaseModel):
    type: Literal["Operative", "Inoperative"]
    connector_id: int = 0


class TriggerRequest(BaseModel):
    message: Literal["StatusNotification", "BootNotification", "Heartbeat", "MeterValues"] = "StatusNotification"
    connector_id: int | None = None


def station_json(s: Station, now: float) -> dict:
    return {
        "id": s.station_id,
        "site": s.site,
        "connected": s.connected,
        "heartbeat_age_s": round(max(0.0, now - s.last_seen), 1),
        "vendor": s.vendor,
        "model": s.model,
        "firmware": s.firmware,
        "limit_w": s.limit_w,
        "connectors": [
            {"id": cid, "status": c.status, "error_code": c.error_code, "power_w": c.power_w,
             "energy_wh": c.energy_wh, "transaction_id": c.transaction_id,
             "session_wh": max(0.0, c.energy_wh - c.meter_start_wh)
             if c.transaction_id and c.meter_start_wh is not None else None,
             "status_age_s": round(now - c.status_since) if c.status_since else None}
            for cid, c in sorted(s.connectors.items()) if cid > 0
        ],
    }


def _limit_profile(watts: float) -> dict:
    start = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {
        "charging_profile_id": LIMIT_PROFILE_ID,
        "stack_level": 0,
        "charging_profile_purpose": ChargingProfilePurposeType.charge_point_max_profile,
        "charging_profile_kind": ChargingProfileKindType.absolute,
        "charging_schedule": {
            "charging_rate_unit": ChargingRateUnitType.watts,
            "start_schedule": start,
            "charging_schedule_period": [{"start_period": 0, "limit": watts}],
        },
    }


def create_admin_app(fleet: FleetStore, registry: Registry, clock: Callable[[], float] = time.time,
                     chaos: Chaos | None = None) -> FastAPI:
    app = FastAPI(title="csms-admin", docs_url=None, redoc_url=None)
    chaos = chaos or Chaos()

    def connection_for(station_id: str):
        if fleet.get(station_id) is None:
            raise HTTPException(404, f"unknown station {station_id}")
        conn = registry.get(station_id)
        if conn is None:
            raise HTTPException(409, f"station {station_id} is offline")
        return conn

    async def send(conn, payload, what: str):
        try:
            return await asyncio.wait_for(conn.call(payload), COMMAND_TIMEOUT_S)
        except TimeoutError:
            raise HTTPException(504, f"{what}: station did not answer in {COMMAND_TIMEOUT_S}s") from None

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/fleet")
    async def get_fleet():
        now = clock()
        return {"stations": [station_json(s, now) for s in sorted(fleet.snapshot(), key=lambda s: s.station_id)]}

    @app.get("/stats")
    async def stats():
        return {**chaos.stats.to_json(), "free_vend": chaos.free_vend}

    @app.get("/chaos")
    async def get_chaos():
        return chaos.to_json()

    @app.post("/chaos")
    async def set_chaos(body: ChaosRequest):
        if body.auth_delay_s is not None:
            chaos.auth_delay_s = body.auth_delay_s
        if body.response_delay_s is not None:
            chaos.response_delay_s = body.response_delay_s
        if body.meter_factor is not None:
            chaos.meter_factor = dict(body.meter_factor)
        if body.drop_profiles is not None:
            chaos.drop_profiles = set(body.drop_profiles)
        if body.free_vend is not None:
            chaos.free_vend = body.free_vend
        log.warning("chaos set: %s", chaos.to_json())
        return chaos.to_json()

    @app.post("/stations/{station_id}/availability")
    async def availability(station_id: str, body: AvailabilityRequest):
        payload = call.ChangeAvailability(connector_id=body.connector_id, type=body.type)
        res = await send(connection_for(station_id), payload, "availability")
        log.info("station=%s availability=%s result=%s", station_id, body.type, res.status)
        return {"station": station_id, "status": res.status}

    @app.post("/stations/{station_id}/limit")
    async def set_limit(station_id: str, body: LimitRequest):
        conn = connection_for(station_id)
        if station_id in chaos.drop_profiles:
            # Injected fault: the charger "accepts" the limit but never applies it.
            fleet.set_limit(station_id, body.watts)
            log.warning("station=%s limit=%s dropped by chaos", station_id, body.watts)
            return {"station": station_id, "limit_w": body.watts, "status": "Accepted"}
        if body.watts is None:
            res = await send(conn, call.ClearChargingProfile(id=LIMIT_PROFILE_ID), "clear limit")
            accepted = res.status == "Accepted" or res.status == "Unknown"
        else:
            res = await send(conn, call.SetChargingProfile(connector_id=0, cs_charging_profiles=_limit_profile(body.watts)), "set limit")
            accepted = res.status == "Accepted"
        if accepted:
            fleet.set_limit(station_id, body.watts)
        log.info("station=%s limit=%s result=%s", station_id, body.watts, res.status)
        return {"station": station_id, "limit_w": body.watts, "status": res.status}

    @app.post("/stations/{station_id}/trigger")
    async def trigger(station_id: str, body: TriggerRequest):
        payload = call.TriggerMessage(requested_message=body.message, connector_id=body.connector_id)
        res = await send(connection_for(station_id), payload, "trigger")
        log.info("station=%s trigger=%s result=%s", station_id, body.message, res.status)
        return {"station": station_id, "status": res.status}

    @app.post("/stations/{station_id}/reset")
    async def reset(station_id: str, body: ResetRequest):
        res = await send(connection_for(station_id), call.Reset(type=body.type), "reset")
        log.info("station=%s reset=%s result=%s", station_id, body.type, res.status)
        return {"station": station_id, "status": res.status}

    return app
