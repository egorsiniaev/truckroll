from control.incidents import (FAULT_RESET_AFTER_S, STATION_OFFLINE_AFTER_S, TRUCK_ROLL_AFTER_RESET_S,
                               VERIFY_AFTER_FIX_S, IncidentManager)


class Csms:
    def __init__(self):
        self.resets = []

    async def reset(self, sid, kind="Soft"):
        self.resets.append(sid)


class Clients:
    def __init__(self):
        self.csms = Csms()


SITES = [{"code": "S"}, {"code": "T"}]


def station(sid, state="available", site="S", connected=True, codes=("HighTemperature",)):
    connectors = [{"status": "Faulted" if state == "faulted" else "Available", "error_code": c} for c in codes]
    return {"id": sid, "site": site, "state": state, "connected": connected and state != "offline",
            "connectors": connectors}


def snap(stations):
    sites = {}
    for st in stations:
        row = sites.setdefault(st["site"], {"code": st["site"], "station_ids": [], "states": {}})
        row["station_ids"].append(st["id"])
        row["states"][st["state"]] = row["states"].get(st["state"], 0) + 1
    return {"stations": stations, "sites": list(sites.values())}


def manager():
    t = [0.0]
    clients = Clients()
    return IncidentManager(SITES, clients, clock=lambda: t[0]), t, clients


def timeline(inc):
    return [(e["type"], e["actor"]) for e in inc["timeline"]]


async def test_fault_reset_fixes_it_after_a_watch_period():
    m, t, clients = manager()
    await m.observe(snap([station("S-1", "faulted"), station("S-2")]))
    t[0] = FAULT_RESET_AFTER_S
    await m.observe(snap([station("S-1", "faulted"), station("S-2")]))
    assert clients.csms.resets == ["S-1"]
    t[0] += 10
    await m.observe(snap([station("S-1"), station("S-2")]))
    assert m.snapshot()[0]["status"] == "monitoring"
    t[0] += VERIFY_AFTER_FIX_S
    await m.observe(snap([station("S-1"), station("S-2")]))
    [inc] = m.snapshot()
    assert inc["status"] == "recovered" and "no truck roll needed" in inc["timeline"][-1]["text"]
    assert [e["type"] for e in inc["timeline"]] == ["detected", "response", "response", "update", "recovered"]


async def test_fault_that_returns_while_watching_escalates_the_same_incident():
    m, t, clients = manager()
    await m.observe(snap([station("S-1", "faulted"), station("S-2")]))
    t[0] = FAULT_RESET_AFTER_S
    await m.observe(snap([station("S-1", "faulted"), station("S-2")]))
    t[0] += 5
    await m.observe(snap([station("S-1"), station("S-2")]))
    t[0] += 30
    await m.observe(snap([station("S-1", "faulted"), station("S-2")]))
    [inc] = m.snapshot()
    assert inc["status"] == "open" and "Truck roll" in inc["timeline"][-1]["text"]
    t[0] += TRUCK_ROLL_AFTER_RESET_S * 2
    await m.observe(snap([station("S-1", "faulted"), station("S-2")]))
    assert clients.csms.resets == ["S-1"]  # no second reset
    await m.observe(snap([station("S-1"), station("S-2")]))
    assert "technician" in m.snapshot()[0]["timeline"][-1]["text"]


async def test_site_outage_is_one_incident_not_many():
    m, t, _ = manager()
    online = [station(f"S-{i}") for i in range(4)] + [station("T-1", site="T")]
    await m.observe(snap(online))
    down = [station(f"S-{i}", "offline") for i in range(4)] + [station("T-1", site="T")]
    for step in range(3):
        t[0] = step * STATION_OFFLINE_AFTER_S
        await m.observe(snap(down))
    incs = m.snapshot()
    assert [i["kind"] for i in incs] == ["site_outage"]
    await m.observe(snap(online))
    assert m.snapshot()[0]["status"] == "recovered"


async def test_single_station_offline_after_grace_period():
    m, t, _ = manager()
    base = [station("S-1"), station("S-2"), station("S-3")]
    await m.observe(snap(base))
    one_down = [station("S-1", "offline"), station("S-2"), station("S-3")]
    await m.observe(snap(one_down))
    assert m.snapshot() == []
    t[0] = STATION_OFFLINE_AFTER_S
    await m.observe(snap(one_down))
    assert m.snapshot()[0]["kind"] == "station_offline"


async def test_flapping_station_is_a_crash_loop():
    m, t, _ = manager()
    others = [station("S-2"), station("S-3"), station("S-4")]
    for k in range(3):
        t[0] = k * 20
        await m.observe(snap([station("S-1")] + others))
        t[0] += 5
        await m.observe(snap([station("S-1", "offline")] + others))
    assert m.snapshot()[0]["kind"] == "crash_loop"


async def test_grid_cap_and_flex_lifecycle():
    m, _, _ = manager()
    m.grid_cap({"site": "S", "kind": "limited", "demand_w": 700000, "available_w": 600000, "share_w": 70000,
                "stations": 8})
    m.grid_cap({"site": "S", "kind": "released", "demand_w": 300000, "available_w": 600000, "peak_w": 710000})
    m.flex_request("T", True, 20)
    m.flex_request("T", False)
    kinds = sorted((i["kind"], i["status"]) for i in m.snapshot())
    assert kinds == [("flex_request", "recovered"), ("grid_cap", "recovered")]
