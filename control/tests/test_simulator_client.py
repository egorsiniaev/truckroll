import json

import pytest
from websockets.asyncio.server import serve

from control.clients import SimulatorClient, UpstreamError

STATIONS = {"status": "success", "chargingStations": [
    {"stationInfo": {"chargingStationId": "A2-HUB-00001", "hashId": "hash-1"}}]}


@pytest.fixture
async def fake_simulator():
    received = []

    async def handler(ws):
        async for raw in ws:
            request_id, procedure, payload = json.loads(raw)
            received.append((procedure, payload))
            if procedure == "listChargingStations":
                await ws.send(json.dumps(["unrelated-id", {"status": "success"}]))
                await ws.send(json.dumps([request_id, STATIONS]))
            elif payload.get("connectorId") == 99:
                await ws.send(json.dumps([request_id, {"status": "failure"}]))
            else:
                await ws.send(json.dumps([request_id, {"status": "success"}]))

    async with serve(handler, "127.0.0.1", 0, subprotocols=["ui0.0.1"]) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        yield f"ws://127.0.0.1:{port}", received


async def test_status_uses_hash_id_and_caches_lookup(fake_simulator):
    url, received = fake_simulator
    sim = SimulatorClient(url)
    await sim.set_status("A2-HUB-00001", 1, "Faulted", "HighTemperature")
    await sim.set_running("A2-HUB-00001", False)

    assert [r[0] for r in received] == ["listChargingStations", "statusNotification", "stopChargingStation"]
    assert received[1][1] == {"hashIds": ["hash-1"], "connectorId": 1, "status": "Faulted", "errorCode": "HighTemperature"}


async def test_unknown_station_and_failure_raise(fake_simulator):
    url, _ = fake_simulator
    sim = SimulatorClient(url)
    with pytest.raises(UpstreamError, match="does not know"):
        await sim.set_running("NOPE-1", True)
    with pytest.raises(UpstreamError, match="failed"):
        await sim.set_status("A2-HUB-00001", 99, "Faulted", "OtherError")


async def test_unreachable_simulator_raises():
    with pytest.raises(UpstreamError):
        await SimulatorClient("ws://127.0.0.1:1").set_running("A2-HUB-00001", True)
