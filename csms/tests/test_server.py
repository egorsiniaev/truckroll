import asyncio
import contextlib

import pytest
from ocpp.routing import on
from ocpp.v16 import ChargePoint as ClientChargePoint
from ocpp.v16 import call, call_result
from ocpp.v16.enums import Action, AuthorizationStatus, RegistrationStatus, TriggerMessageStatus
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from csms.auth import TagList
from csms.fleet import FleetStore
from csms.server import serve
from csms.telemetry import Telemetry

STATION = "AMS-DEPOT-00001"


@pytest.fixture
def reader():
    return InMemoryMetricReader()


@pytest.fixture
def fleet():
    return FleetStore()


@pytest.fixture
async def server(fleet, reader):
    telemetry = Telemetry(MeterProvider(metric_readers=[reader]).get_meter("test"), fleet)
    srv = await serve(
        host="127.0.0.1",
        port=0,
        fleet=fleet,
        telemetry=telemetry,
        auth=TagList(frozenset({"GOOD-TAG"})),
        heartbeat_interval_s=30,
        resync_delay_s=0.05,
    )
    port = next(iter(srv.sockets)).getsockname()[1]
    yield f"ws://127.0.0.1:{port}"
    srv.close()
    await srv.wait_closed()


class RecordingStation(ClientChargePoint):
    triggers: list

    @on(Action.trigger_message)
    async def on_trigger(self, requested_message, **kwargs):
        self.triggers.append(requested_message)
        return call_result.TriggerMessage(status=TriggerMessageStatus.accepted)


@contextlib.asynccontextmanager
async def station(url, station_id=STATION):
    async with connect(f"{url}/{station_id}", subprotocols=["ocpp1.6"]) as ws:
        cp = RecordingStation(station_id, ws)
        cp.triggers = []
        task = asyncio.create_task(cp.start())
        try:
            yield cp
        finally:
            task.cancel()


def metric_points(reader):
    points = {}
    data = reader.get_metrics_data()
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                points.setdefault(m.name, []).extend(m.data.data_points)
    return points


async def test_boot_notification_is_accepted_with_configured_interval(server, fleet):
    async with station(server) as cp:
        res = await cp.call(call.BootNotification(charge_point_model="AC22", charge_point_vendor="Truckroll"))

    assert res.status == RegistrationStatus.accepted
    assert res.interval == 30
    assert fleet.get(STATION).model == "AC22"


async def test_station_is_marked_offline_after_disconnect(server, fleet):
    async with station(server) as cp:
        await cp.call(call.Heartbeat())
        assert fleet.get(STATION).connected is True
    await asyncio.sleep(0.1)
    assert fleet.get(STATION).connected is False


async def test_authorize_accepts_known_tag_and_rejects_unknown(server):
    async with station(server) as cp:
        good = await cp.call(call.Authorize(id_tag="GOOD-TAG"))
        bad = await cp.call(call.Authorize(id_tag="STOLEN"))

    assert good.id_tag_info["status"] == AuthorizationStatus.accepted
    assert bad.id_tag_info["status"] == AuthorizationStatus.invalid


async def test_session_start_and_stop_are_tracked_and_counted(server, fleet, reader):
    async with station(server) as cp:
        await cp.call(call.StatusNotification(connector_id=1, error_code="NoError", status="Charging"))
        started = await cp.call(
            call.StartTransaction(connector_id=1, id_tag="GOOD-TAG", meter_start=0, timestamp="2026-11-24T10:00:00Z")
        )
        assert started.id_tag_info["status"] == AuthorizationStatus.accepted
        assert fleet.get(STATION).connectors[1].transaction_id == started.transaction_id

        await cp.call(
            call.MeterValues(
                connector_id=1,
                transaction_id=started.transaction_id,
                meter_value=[
                    {
                        "timestamp": "2026-11-24T10:01:00Z",
                        "sampled_value": [
                            {"value": "7.4", "measurand": "Power.Active.Import", "unit": "kW"},
                            {"value": "250", "measurand": "Energy.Active.Import.Register", "unit": "Wh"},
                        ],
                    }
                ],
            )
        )
        assert fleet.get(STATION).connectors[1].power_w == pytest.approx(7400.0)

        await cp.call(call.StopTransaction(meter_stop=250, timestamp="2026-11-24T10:05:00Z", transaction_id=started.transaction_id))

    points = metric_points(reader)
    assert sum(p.value for p in points["csms.sessions.started"]) == 1
    assert sum(p.value for p in points["csms.sessions.stopped"]) == 1
    actions = {p.attributes["action"] for p in points["ocpp.messages"]}
    assert {"StartTransaction", "MeterValues", "StopTransaction"} <= actions


async def test_session_with_unknown_tag_is_rejected_and_counted(server, fleet, reader):
    async with station(server) as cp:
        res = await cp.call(
            call.StartTransaction(connector_id=1, id_tag="STOLEN", meter_start=0, timestamp="2026-11-24T10:00:00Z")
        )

    assert res.id_tag_info["status"] == AuthorizationStatus.invalid
    assert fleet.get(STATION).connectors.get(1) is None or fleet.get(STATION).connectors[1].transaction_id is None
    rejected = metric_points(reader)["csms.sessions.rejected"]
    assert rejected[0].attributes == {"site": "AMS-DEPOT", "reason": "Invalid"}


async def test_connector_status_gauge_reports_current_status_only(server, reader):
    async with station(server) as cp:
        await cp.call(call.StatusNotification(connector_id=1, error_code="NoError", status="Available"))
        await cp.call(call.StatusNotification(connector_id=1, error_code="HighTemperature", status="Faulted"))
        points = metric_points(reader)

    current = [p for p in points["csms.connector.status"] if p.value == 1]
    assert [p.attributes["status"] for p in current] == ["Faulted"]
    assert any(p.attributes["status"] == "Available" and p.value == 0 for p in points["csms.connector.status"])
    errors = [p for p in points["csms.connector.error"] if p.value == 1]
    assert [p.attributes["error_code"] for p in errors] == ["HighTemperature"]


async def test_connection_without_ocpp16_subprotocol_is_refused(server, fleet):
    with pytest.raises(WebSocketException):
        async with connect(f"{server}/{STATION}") as ws:
            await ws.recv()
    assert fleet.get(STATION) is None


async def test_station_that_reconnects_without_booting_is_asked_to_boot(server):
    async with station(server) as cp:
        await asyncio.sleep(0.3)
    assert cp.triggers == ["BootNotification"]


async def test_booted_station_is_not_asked_to_boot(server):
    async with station(server) as cp:
        await cp.call(call.BootNotification(charge_point_model="AC22", charge_point_vendor="Truckroll"))
        await asyncio.sleep(0.3)
    assert cp.triggers == []


async def test_reboot_and_available_close_stale_transactions(server, fleet):
    async with station(server) as cp:
        await cp.call(call.BootNotification(charge_point_model="AC22", charge_point_vendor="Truckroll"))
        started = await cp.call(
            call.StartTransaction(connector_id=1, id_tag="GOOD-TAG", meter_start=0, timestamp="2026-11-24T10:00:00Z")
        )
        await cp.call(call.StatusNotification(connector_id=1, error_code="NoError", status="Available"))
        assert fleet.get(STATION).connectors[1].transaction_id is None

        await cp.call(call.StartTransaction(connector_id=1, id_tag="GOOD-TAG", meter_start=0, timestamp="2026-11-24T10:01:00Z"))
        await cp.call(call.BootNotification(charge_point_model="AC22", charge_point_vendor="Truckroll"))
        assert fleet.get(STATION).connectors[1].transaction_id is None
    assert started.transaction_id


async def test_meter_values_with_unknown_transaction_adopt_it(server, fleet):
    async with station(server) as cp:
        await cp.call(call.MeterValues(connector_id=1, transaction_id=461, meter_value=[
            {"timestamp": "2026-11-24T10:01:00Z", "sampled_value": [{"value": "100", "unit": "Wh"}]}]))
        assert fleet.get(STATION).connectors[1].transaction_id == 461
        await cp.call(call.StopTransaction(meter_stop=200, timestamp="2026-11-24T10:05:00Z", transaction_id=461))
        assert fleet.get(STATION).connectors[1].transaction_id is None


async def test_meter_values_from_an_ended_session_do_not_overwrite_the_current_one(server, fleet):
    async with station(server) as cp:
        old = await cp.call(call.StartTransaction(connector_id=1, id_tag="GOOD-TAG", meter_start=0, timestamp="2026-11-24T10:00:00Z"))
        await cp.call(call.StopTransaction(meter_stop=20000, timestamp="2026-11-24T10:30:00Z", transaction_id=old.transaction_id))
        new = await cp.call(call.StartTransaction(connector_id=1, id_tag="GOOD-TAG", meter_start=0, timestamp="2026-11-24T10:31:00Z"))
        await cp.call(call.MeterValues(connector_id=1, transaction_id=new.transaction_id, meter_value=[
            {"timestamp": "t", "sampled_value": [{"value": "100", "unit": "Wh"}]}]))
        await cp.call(call.MeterValues(connector_id=1, transaction_id=old.transaction_id, meter_value=[
            {"timestamp": "t", "sampled_value": [{"value": "20000", "unit": "Wh"}]}]))
        assert fleet.get(STATION).connectors[1].energy_wh == 100


async def test_late_meter_values_do_not_revive_a_closed_transaction(server, fleet):
    async with station(server) as cp:
        started = await cp.call(call.StartTransaction(connector_id=1, id_tag="GOOD-TAG", meter_start=0, timestamp="2026-11-24T10:00:00Z"))
        await cp.call(call.StatusNotification(connector_id=1, error_code="NoError", status="Available"))
        await cp.call(call.MeterValues(connector_id=1, transaction_id=started.transaction_id, meter_value=[
            {"timestamp": "t", "sampled_value": [{"value": "5000", "unit": "Wh"}]}]))
        conn = fleet.get(STATION).connectors[1]
        assert conn.transaction_id is None
        assert conn.power_w == 0.0


async def test_session_closed_by_a_resync_boot_is_taken_back_while_the_charger_still_charges(server, fleet):
    async with station(server) as cp:
        started = await cp.call(call.StartTransaction(connector_id=1, id_tag="GOOD-TAG", meter_start=0, timestamp="2026-11-24T10:00:00Z"))
        await cp.call(call.BootNotification(charge_point_model="AC22", charge_point_vendor="Truckroll"))
        await cp.call(call.StatusNotification(connector_id=1, error_code="NoError", status="Charging"))
        await cp.call(call.MeterValues(connector_id=1, transaction_id=started.transaction_id, meter_value=[
            {"timestamp": "t", "sampled_value": [{"value": "5000", "unit": "Wh"}]}]))
        assert fleet.get(STATION).connectors[1].transaction_id == started.transaction_id
