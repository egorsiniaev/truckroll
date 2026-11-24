import asyncio
import json

import pytest

from control.limits import LimitArbiter
from control.scenarios import SCENARIOS, Ledger, Runner

SITES = [{"code": c, "proxy": c.lower(), "cap_w": 200000} for c in ("AMS-DEPOT", "UTR-RETAIL", "A2-HUB", "EHV-OFFICE")]
STATIONS = [{"id": f"{s['code']}-{i}", "site": s["code"], "connected": True,
             "connectors": [{"id": 1}, {"id": 2}]} for s in SITES for i in range(1, 5)]


class Recorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        async def record(*args):
            self.calls.append((name, *args))
        return record


class Clients:
    def __init__(self):
        rec = Recorder()
        self.simulator = self.csms = self.toxiproxy = rec
        self.calls = rec.calls


@pytest.fixture
def runner(tmp_path):
    events = []
    clients = Clients()
    r = Runner(clients, SITES, Ledger(tmp_path / "ledger.jsonl"), lambda lvl, txt: events.append((lvl, txt)),
               time_scale=0.001, seed=7, limits=LimitArbiter(clients.csms))
    return r, clients, events, tmp_path / "ledger.jsonl"


@pytest.mark.parametrize("scenario", [s.id for s in SCENARIOS])
async def test_every_scenario_runs_cleans_up_and_writes_ledger(runner, scenario):
    r, clients, events, ledger = runner
    r.start(scenario, STATIONS)
    await r.task
    record = json.loads(ledger.read_text().splitlines()[-1])
    assert record["scenario"] == scenario and record["outcome"] == "completed"
    assert record["root_cause"]
    assert clients.calls, "scenario did nothing"
    assert events[-1] == ("ok", f"scenario completed: {next(s.title for s in SCENARIOS if s.id == scenario)}")
    assert r.status() == {"running": None}


async def test_overheat_faults_then_clears_the_same_station(runner):
    r, clients, _, ledger = runner
    r.start("overheat", STATIONS)
    await r.task
    faulted = {c[1] for c in clients.calls if c[0] == "set_status" and c[3] == "Faulted"}
    cleared = {c[1] for c in clients.calls if c[0] == "set_status" and c[3] == "Available"}
    assert len(faulted) == 1 and faulted == cleared
    assert json.loads(ledger.read_text())["targets"]["hot"] == sorted(faulted)


async def test_stop_runs_cleanup_and_logs_stopped(tmp_path):
    clients = Clients()
    r = Runner(clients, SITES, Ledger(tmp_path / "l.jsonl"), lambda *a: None, time_scale=1.0)
    r.start("site-drop", STATIONS)
    await asyncio.sleep(0.05)
    await r.stop()
    assert ("set_enabled", "ams-depot", False) in clients.calls
    assert clients.calls[-1] in {("set_latency", "ams-depot", 0)}
    assert json.loads((tmp_path / "l.jsonl").read_text())["outcome"] == "stopped"


async def test_only_one_scenario_at_a_time(runner):
    r, *_ = runner
    r.time_scale = 1.0
    r.start("site-drop", STATIONS)
    with pytest.raises(RuntimeError):
        r.start("storm-a2", STATIONS)
    await r.stop()


async def test_unknown_scenario(runner):
    with pytest.raises(KeyError):
        runner[0].start("nope", STATIONS)


async def test_status_reports_icon_sites_and_picked_stations(runner):
    r, *_ = runner
    r.time_scale = 1.0
    r.start("overheat", STATIONS)
    await asyncio.sleep(0.05)
    run = r.status()["running"]
    assert run["icon"] == "🌡️" and run["sites"] == ["A2-HUB"] and len(run["stations"]) == 1
    await r.stop()
    assert all(s["icon"] for s in r.catalog())
