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

    async def stats(self):
        return {"reply_latency_s": 0.0, "auth_latency_s": 0.0, "free_vend": False}

    async def chaos(self, **kw):
        self.calls.append(("chaos", kw))
        return {}


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

    async def start_transaction(self, sid, cid, tag):
        pass  # demand engine traffic; not what these tests check

    async def stop_transaction(self, sid, tx):
        pass


@pytest.fixture
async def ctx():
    clients = Clients(csms=FakeCsms(), toxiproxy=FakeToxi(), simulator=FakeSim(), weather=None)
    hub = Hub(sites=SITES, clients=clients, ledger_path=None)
    await hub.poll_once()
    clients.toxiproxy.calls.clear()  # first poll runs the startup cleanup
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
    assert hub.snapshot["events"][0]["text"] == "operator started a network outage"


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


async def test_limit_power_and_reset_are_forwarded(ctx):  # noqa: D103
    client, clients, _ = ctx
    await client.post("/api/stations/A2-HUB-00001/limit", json={"watts": 75000})
    await client.post("/api/stations/A2-HUB-00001/reset")
    await client.post("/api/stations/A2-HUB-00001/power", json={"on": False})
    assert clients.csms.calls == [("limit", "A2-HUB-00001", 75000), ("reset", "A2-HUB-00001")]
    assert hub_station(ctx)["limit_sources"] == {"operator": 75000}
    assert clients.simulator.calls == [("running", "A2-HUB-00001", False)]


def hub_station(ctx):
    return next(s for s in ctx[2].snapshot["stations"] if s["id"] == "A2-HUB-00001")


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


async def test_snapshot_has_daylight_incidents_autopilot_and_grafana(ctx):
    _, _, hub = ctx
    snap = hub.snapshot
    assert snap["daylight"]["phase"] in {"morning", "day", "evening", "night"}
    assert snap["incidents"] == [] and snap["autopilot"]["enabled"] is False
    assert "grafana_url" in snap


async def test_autopilot_toggle(ctx):
    client, _, hub = ctx
    res = await client.post("/api/autopilot", json={"enabled": True})
    assert res.json()["result"]["enabled"] is True


async def test_metrics_endpoint_and_pricing_toggle(ctx):
    client, _, hub = ctx
    res = await client.get("/metrics")
    assert res.status_code == 200 and "truckroll_site_power_watts" in res.text
    await client.post("/api/pricing", json={"enabled": False})
    assert hub.economics.dynamic is False


async def test_uptime_money_and_battery_survive_a_restart(tmp_path):
    from datetime import UTC, datetime
    sites = [{**SITES[0], "demand": "highway"}]

    def make():
        clients = Clients(csms=FakeCsms(), toxiproxy=FakeToxi(), simulator=FakeSim(), weather=None)
        return Hub(sites=sites, clients=clients, ledger_path=tmp_path / "ledger.jsonl")

    hub = make()
    hub.uptime.accrue([{"id": "A2-HUB-00001", "site": "A2-HUB", "connected": False,
                        "connectors": [{"status": "Available"}]}], 600, datetime.now(UTC))
    hub.economics.accrue([{"code": "A2-HUB", "power_w": 10_000}], None, 3600, datetime.now(UTC))
    hub.batteries.batteries["A2-HUB"].soc_kwh = 123.0
    from control.demand import Car
    hub.demand.state["A2-HUB"].queue.append(Car(1, "A2-HUB", 9000, hub.demand.clock(), 600, "T"))
    inc = hub.incidents._new("fault:A2-HUB-00001", "fault", "critical", "boom", "A2-HUB", ["A2-HUB-00001"])
    hub._save_state()

    again = make()
    assert again.uptime.per_site["A2-HUB"].total_s == 600 and again.uptime.summary()["fleet"] == 0.0
    assert again.economics.summary()["fleet"]["energy_kwh"] == 10
    assert again.batteries.batteries["A2-HUB"].soc_kwh == 123.0
    assert len(again.demand.state["A2-HUB"].queue) == 1
    assert again.incidents.open["fault:A2-HUB-00001"].id == inc.id


async def test_slo_summary_and_decision_api(ctx):
    client, clients, hub = ctx
    ids = {s["id"] for s in hub.snapshot["slo"]}
    assert ids == {"uptime", "uptime_excl", "starts", "cap", "flex", "ttd", "ttm", "ttr_remote", "ttr_field", "answered"}
    assert (await client.post("/api/decisions/DEC-9999", json={"choice": "approve"})).status_code == 404
    inc = hub.incidents._new("x", "fault", "critical", "t", "A2-HUB", ["A2-HUB-00001"])
    await hub.incidents._ask(inc, "reset", {"station": "A2-HUB-00001"}, "Reset it?", "Because.")
    [d] = hub.book.snapshot()["pending"]
    res = await client.post(f"/api/decisions/{d['id']}", json={"choice": "approve"})
    assert res.status_code == 200
    assert ("reset", "A2-HUB-00001") in clients.csms.calls
    assert inc.timeline[-1]["actor"] == "operator" and "approved" in inc.timeline[-1]["text"]
    assert (await client.post("/api/hitl", json={"enabled": False})).json()["result"]["enabled"] is False
