import httpx
import pytest

from control.app import Clients, Hub, create_app
from control.clients import UpstreamError

SITES = [{"code": "A2-HUB", "kind": "hub", "stations": 1, "connectors": 2, "power_w": 150000, "current": "DC",
          "lat": 52.0, "lon": 5.0, "cap_w": 300000, "proxy": "a2-hub"}]
STATION = {"id": "A2-HUB-00001", "site": "A2-HUB", "connected": True, "heartbeat_age_s": 3, "limit_w": None,
           "vendor": "v", "model": "m", "firmware": "f",
           "connectors": [{"id": 1, "status": "Charging", "error_code": "NoError", "power_w": 1000, "energy_wh": 5,
                           "transaction_id": 1},
                          {"id": 2, "status": "Available", "error_code": "NoError", "power_w": 0, "energy_wh": 0,
                           "transaction_id": None}]}


class FakeCsms:
    def __init__(self):
        self.calls = []

    async def fleet(self):
        return [STATION]

    async def set_limit(self, sid, watts):
        self.calls.append(("limit", sid, watts))
        return {"status": "Accepted"}

    async def reset(self, sid, kind="Soft"):
        self.calls.append(("reset", sid))
        return {"status": "Accepted"}


class FakeToxi:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def proxies(self):
        return {"a2-hub": {"enabled": True, "toxics": []}}

    async def set_enabled(self, proxy, enabled):
        if self.fail:
            raise UpstreamError("toxiproxy down")
        self.calls.append(("enabled", proxy, enabled))

    async def set_latency(self, proxy, ms):
        self.calls.append(("latency", proxy, ms))


class FakeSim:
    def __init__(self):
        self.calls = []

    async def set_status(self, sid, cid, status, code):
        self.calls.append(("status", sid, cid, status, code))

    async def set_running(self, sid, running):
        self.calls.append(("running", sid, running))

    async def set_atg_many(self, ids, running):
        self.calls.append(("atg", tuple(ids), running))


@pytest.fixture
async def ctx():
    clients = Clients(csms=FakeCsms(), toxiproxy=FakeToxi(), simulator=FakeSim(), weather=None)
    hub = Hub(sites=SITES, clients=clients, ledger_path=None)
    await hub.poll_once()
    app = create_app(hub, web_dir=None, start_poller=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        yield client, clients, hub


async def test_state_contains_sites_stations_and_kpis(ctx):
    client, _, _ = ctx
    body = (await client.get("/api/state")).json()
    assert body["kpis"]["charging"] == 1
    assert body["sites"][0]["code"] == "A2-HUB"
    assert body["stations"][0]["state"] == "charging"


async def test_outage_disables_the_site_proxy_and_logs_event(ctx):
    client, clients, hub = ctx
    res = await client.post("/api/sites/A2-HUB/outage", json={"on": True})
    assert res.status_code == 200
    assert clients.toxiproxy.calls == [("enabled", "a2-hub", False)]
    assert hub.snapshot["events"][0]["text"] == "network outage started"


async def test_latency_and_clear_chaos(ctx):
    client, clients, _ = ctx
    await client.post("/api/sites/A2-HUB/latency", json={"ms": 1500})
    await client.post("/api/chaos/clear")
    assert clients.toxiproxy.calls == [("latency", "a2-hub", 1500), ("enabled", "a2-hub", True), ("latency", "a2-hub", 0)]


async def test_fault_is_injected_on_every_connector_then_cleared(ctx):
    client, clients, _ = ctx
    await client.post("/api/stations/A2-HUB-00001/fault", json={"error_code": "HighTemperature"})
    await client.post("/api/stations/A2-HUB-00001/clear-fault")
    assert clients.simulator.calls == [
        ("status", "A2-HUB-00001", 1, "Faulted", "HighTemperature"),
        ("status", "A2-HUB-00001", 2, "Faulted", "HighTemperature"),
        ("status", "A2-HUB-00001", 1, "Available", "NoError"),
        ("status", "A2-HUB-00001", 2, "Available", "NoError"),
    ]


async def test_limit_power_and_reset_are_forwarded(ctx):
    client, clients, _ = ctx
    await client.post("/api/stations/A2-HUB-00001/limit", json={"watts": 75000})
    await client.post("/api/stations/A2-HUB-00001/reset")
    await client.post("/api/stations/A2-HUB-00001/power", json={"on": False})
    assert clients.csms.calls == [("limit", "A2-HUB-00001", 75000), ("reset", "A2-HUB-00001")]
    assert clients.simulator.calls == [("running", "A2-HUB-00001", False)]


@pytest.mark.parametrize("path,body", [
    ("/api/sites/NOPE/outage", {"on": True}),
    ("/api/stations/NOPE-1/limit", {"watts": 1}),
])
async def test_unknown_targets_are_404(ctx, path, body):
    client, _, _ = ctx
    assert (await client.post(path, json=body)).status_code == 404


async def test_invalid_input_is_422(ctx):
    client, _, _ = ctx
    assert (await client.post("/api/stations/A2-HUB-00001/fault", json={"error_code": "Bananas"})).status_code == 422
    assert (await client.post("/api/sites/A2-HUB/latency", json={"ms": -1})).status_code == 422


async def test_upstream_failure_is_502_and_logged(ctx):
    client, clients, hub = ctx
    clients.toxiproxy.fail = True
    res = await client.post("/api/sites/A2-HUB/outage", json={"on": True})
    assert res.status_code == 502
    assert hub.events[0]["level"] == "error"


async def test_meta_lists_scenarios_and_world_toggle_works(ctx):
    client, clients, hub = ctx
    meta = (await client.get("/api/meta")).json()
    assert {s["id"] for s in meta["scenarios"]} >= {"storm-a2", "overheat"}
    assert (await client.post("/api/world", json={"enabled": True})).status_code == 200
    assert hub.snapshot["world"]["enabled"] is True
    assert (await client.post("/api/world", json={"enabled": False})).status_code == 200
    assert hub.snapshot["world"]["enabled"] is False


async def test_scenario_start_conflict_unknown_and_stop(ctx):
    client, clients, hub = ctx
    assert (await client.post("/api/scenarios/nope/start")).status_code == 404
    res = await client.post("/api/scenarios/site-drop/start")
    assert res.json()["result"]["running"]["id"] == "site-drop"
    assert (await client.post("/api/scenarios/storm-a2/start")).status_code == 409
    stopped = await client.post("/api/scenarios/stop")
    assert stopped.json()["result"] == {"running": None}
