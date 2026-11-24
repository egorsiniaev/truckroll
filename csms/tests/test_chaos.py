import asyncio
import contextlib
import time

import httpx
import pytest
from ocpp.routing import on
from ocpp.v16 import ChargePoint as ClientChargePoint
from ocpp.v16 import call, call_result
from ocpp.v16.enums import Action, AuthorizationStatus
from opentelemetry.sdk.metrics import MeterProvider
from websockets.asyncio.client import connect

from csms.admin import create_admin_app
from csms.auth import TagList
from csms.chaos import Chaos
from csms.fleet import FleetStore
from csms.registry import Registry
from csms.server import serve
from csms.telemetry import Telemetry

STATION = "S-00001"


class Station(ClientChargePoint):
    got: list

    @on(Action.set_charging_profile)
    async def on_profile(self, connector_id, cs_charging_profiles, **kw):
        self.got.append("profile")
        return call_result.SetChargingProfile(status="Accepted")

    @on(Action.change_availability)
    async def on_avail(self, connector_id, type, **kw):
        self.got.append(("availability", type))
        return call_result.ChangeAvailability(status="Accepted")

    @on(Action.trigger_message)
    async def on_trigger(self, requested_message, **kw):
        return call_result.TriggerMessage(status="Accepted")


@pytest.fixture
async def stack():
    fleet, registry, chaos = FleetStore(), Registry(), Chaos()
    tel = Telemetry(MeterProvider().get_meter("t"), fleet)
    srv = await serve("127.0.0.1", 0, fleet, tel, TagList(frozenset({"OK"})), 30, resync_delay_s=10,
                      registry=registry, chaos=chaos)
    port = next(iter(srv.sockets)).getsockname()[1]
    api = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_admin_app(fleet, registry, chaos=chaos)),
                            base_url="http://a")
    yield f"ws://127.0.0.1:{port}", api, fleet, chaos
    await api.aclose()
    srv.close()
    await srv.wait_closed()


@contextlib.asynccontextmanager
async def station(url):
    async with connect(f"{url}/{STATION}", subprotocols=["ocpp1.6"]) as ws:
        cp = Station(STATION, ws)
        cp.got = []
        task = asyncio.create_task(cp.start())
        await cp.call(call.BootNotification(charge_point_model="M", charge_point_vendor="V"))
        try:
            yield cp
        finally:
            task.cancel()


async def test_auth_delay_and_free_vend(stack):
    url, api, _, chaos = stack
    async with station(url) as cp:
        await api.post("/chaos", json={"auth_delay_s": 0.3})
        t0 = time.monotonic()
        res = await cp.call(call.Authorize(id_tag="UNKNOWN"))
        assert time.monotonic() - t0 >= 0.3 and res.id_tag_info["status"] == AuthorizationStatus.invalid
        assert (await api.get("/stats")).json()["auth_latency_s"] > 0
        await api.post("/chaos", json={"free_vend": True})
        t0 = time.monotonic()
        assert (await cp.call(call.Authorize(id_tag="UNKNOWN"))).id_tag_info["status"] == AuthorizationStatus.accepted
        assert time.monotonic() - t0 < 0.2  # free vend doesn't wait for the slow backend


async def test_meter_drift_scales_energy_from_session_start(stack):
    url, api, fleet, _ = stack
    async with station(url) as cp:
        started = await cp.call(call.StartTransaction(connector_id=1, id_tag="OK", meter_start=1000, timestamp="2026-11-24T10:00:00Z"))
        await api.post("/chaos", json={"meter_factor": {STATION: 1.5}})
        await cp.call(call.MeterValues(connector_id=1, transaction_id=started.transaction_id, meter_value=[
            {"timestamp": "t", "sampled_value": [{"value": "3000", "unit": "Wh"}]}]))
    assert fleet.get(STATION).connectors[1].energy_wh == 1000 + 2000 * 1.5


async def test_dropped_profiles_look_accepted_but_never_reach_the_station(stack):
    url, api, fleet, _ = stack
    async with station(url) as cp:
        await api.post("/chaos", json={"drop_profiles": [STATION]})
        res = await api.post(f"/stations/{STATION}/limit", json={"watts": 5000})
        assert res.json()["status"] == "Accepted" and fleet.get(STATION).limit_w == 5000
        assert cp.got == []
        await api.post(f"/stations/{STATION}/availability", json={"type": "Inoperative"})
        assert cp.got == [("availability", "Inoperative")]
