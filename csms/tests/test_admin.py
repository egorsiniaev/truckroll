import asyncio
import contextlib

import httpx
import pytest
from ocpp.routing import on
from ocpp.v16 import ChargePoint as ClientChargePoint
from ocpp.v16 import call, call_result
from ocpp.v16.enums import Action
from opentelemetry.sdk.metrics import MeterProvider
from websockets.asyncio.client import connect

from csms.admin import LIMIT_PROFILE_ID, create_admin_app
from csms.auth import TagList
from csms.fleet import FleetStore
from csms.registry import Registry
from csms.server import serve
from csms.telemetry import Telemetry

STATION = "A2-HUB-00001"


class ObedientStation(ClientChargePoint):
    received: list

    @on(Action.set_charging_profile)
    async def on_set_profile(self, connector_id, cs_charging_profiles, **kwargs):
        self.received.append(("set", connector_id, cs_charging_profiles))
        return call_result.SetChargingProfile(status="Accepted")

    @on(Action.clear_charging_profile)
    async def on_clear_profile(self, **kwargs):
        self.received.append(("clear", kwargs))
        return call_result.ClearChargingProfile(status="Accepted")

    @on(Action.reset)
    async def on_reset(self, type, **kwargs):
        self.received.append(("reset", type))
        return call_result.Reset(status="Accepted")

    @on(Action.trigger_message)
    async def on_trigger(self, requested_message, **kwargs):
        return call_result.TriggerMessage(status="Accepted")


@pytest.fixture
async def stack():
    fleet, registry = FleetStore(), Registry()
    telemetry = Telemetry(MeterProvider().get_meter("t"), fleet)
    srv = await serve("127.0.0.1", 0, fleet, telemetry, TagList(frozenset({"X"})), 30, resync_delay_s=10, registry=registry)
    port = next(iter(srv.sockets)).getsockname()[1]
    api = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_admin_app(fleet, registry)), base_url="http://admin")
    yield f"ws://127.0.0.1:{port}", api, fleet
    await api.aclose()
    srv.close()
    await srv.wait_closed()


@contextlib.asynccontextmanager
async def station(url):
    async with connect(f"{url}/{STATION}", subprotocols=["ocpp1.6"]) as ws:
        cp = ObedientStation(STATION, ws)
        cp.received = []
        task = asyncio.create_task(cp.start())
        await cp.call(call.BootNotification(charge_point_model="DC150", charge_point_vendor="Truckroll"))
        try:
            yield cp
        finally:
            task.cancel()


async def test_fleet_endpoint_lists_connected_station(stack):
    url, api, _ = stack
    async with station(url) as cp:
        await cp.call(call.StatusNotification(connector_id=1, error_code="NoError", status="Charging"))
        body = (await api.get("/fleet")).json()

    [s] = body["stations"]
    assert s["id"] == STATION and s["site"] == "A2-HUB" and s["connected"] is True
    assert s["connectors"][0]["status"] == "Charging"


async def test_limit_sends_station_wide_profile_and_is_recorded(stack):
    url, api, fleet = stack
    async with station(url) as cp:
        res = await api.post(f"/stations/{STATION}/limit", json={"watts": 50000})

    assert res.status_code == 200 and res.json()["status"] == "Accepted"
    kind, connector_id, profile = cp.received[0]
    assert (kind, connector_id) == ("set", 0)
    assert profile["charging_profile_id"] == LIMIT_PROFILE_ID
    assert profile["charging_profile_purpose"] == "ChargePointMaxProfile"
    assert profile["charging_schedule"]["charging_schedule_period"][0]["limit"] == 50000
    assert fleet.get(STATION).limit_w == 50000


async def test_clearing_limit_sends_clear_profile(stack):
    url, api, fleet = stack
    async with station(url) as cp:
        await api.post(f"/stations/{STATION}/limit", json={"watts": 50000})
        await api.post(f"/stations/{STATION}/limit", json={"watts": None})

    assert cp.received[1][0] == "clear"
    assert fleet.get(STATION).limit_w is None


async def test_reset_is_forwarded(stack):
    url, api, _ = stack
    async with station(url) as cp:
        res = await api.post(f"/stations/{STATION}/reset", json={"type": "Hard"})
    assert res.json()["status"] == "Accepted"
    assert cp.received == [("reset", "Hard")]


async def test_unknown_station_is_404_and_offline_station_is_409(stack):
    url, api, _ = stack
    assert (await api.post("/stations/NOPE-1/limit", json={"watts": 1})).status_code == 404
    async with station(url):
        pass
    await asyncio.sleep(0.1)
    assert (await api.post(f"/stations/{STATION}/limit", json={"watts": 1})).status_code == 409


async def test_negative_limit_is_rejected(stack):
    _, api, _ = stack
    assert (await api.post(f"/stations/{STATION}/limit", json={"watts": -5})).status_code == 422


async def test_fleet_reports_energy_this_session(stack):
    url, api, _ = stack
    async with station(url) as cp:
        started = await cp.call(call.StartTransaction(connector_id=1, id_tag="X", meter_start=1000, timestamp="2026-11-24T10:00:00Z"))
        await cp.call(call.MeterValues(connector_id=1, transaction_id=started.transaction_id, meter_value=[
            {"timestamp": "2026-11-24T10:01:00Z", "sampled_value": [{"value": "3500", "measurand": "Energy.Active.Import.Register", "unit": "Wh"}]}]))
        body = (await api.get("/fleet")).json()
    assert body["stations"][0]["connectors"][0]["session_wh"] == 2500
