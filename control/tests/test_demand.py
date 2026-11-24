import random

from control.clients import UpstreamError
from control.demand import START_TIMEOUT_S, DemandEngine

SITE = {"code": "S", "demand": "retail"}


class Sim:
    def __init__(self):
        self.calls, self.fail = [], False

    async def start_transaction(self, sid, cid, tag):
        if self.fail:
            raise UpstreamError("busy")
        self.calls.append(("start", sid, cid))

    async def stop_transaction(self, sid, tx):
        self.calls.append(("stop", sid, tx))


class Clients:
    def __init__(self):
        self.simulator = Sim()


def snap(connectors):
    """connectors: list of (station, connector, status, tx, energy_wh)."""
    stations = {}
    for sid, cid, status, tx, wh in connectors:
        st = stations.setdefault(sid, {"id": sid, "site": "S", "connected": True, "connectors": []})
        st["connectors"].append({"id": cid, "status": status, "transaction_id": tx, "energy_wh": wh})
    return {"stations": list(stations.values())}


def engine():
    t = [0.0]
    lost = []
    eng = DemandEngine([SITE], Clients(), ["TAG"], rng=random.Random(3), clock=lambda: t[0],
                       on_lost=lambda site, kwh: lost.append((site, kwh)))
    eng._warm = True  # tests drive arrivals explicitly
    return eng, t, lost


async def test_warm_start_seeds_half_an_hour_of_arrivals():
    eng = DemandEngine([SITE], Clients(), ["TAG"], rng=random.Random(3), clock=lambda: 0.0)
    await eng.tick(snap([("S-1", 1, "Charging", 5, 0)]), {"S": 1.0}, {"S": 1.0})
    assert len(eng.state["S"].queue) == 15  # 30 cars/h x 0.5 h, all waiting: no free connector


async def test_arrivals_take_free_connectors_and_the_rest_queue():
    eng, t, _ = engine()
    for _ in range(20):
        t[0] += 30
        await eng.tick(snap([("S-1", 1, "Available", None, 0)]), {"S": 1.0}, {"S": 1.0})
    st = eng.state["S"]
    assert eng.clients.simulator.calls[0] == ("start", "S-1", 1)
    assert len(st.sessions) == 1  # only one connector
    assert eng.status()["S"]["arrivals_per_hour"] == 30


async def test_session_stops_when_the_car_has_its_energy():
    eng, t, _ = engine()
    eng.state["S"].queue.append(__import__("control.demand", fromlist=["Car"]).Car(1, "S", 5000, 0, 9999, "TAG"))
    await eng.tick(snap([("S-1", 1, "Available", None, 1000)]), {"S": 0.0}, {"S": 1.0})
    t[0] += 5
    await eng.tick(snap([("S-1", 1, "Charging", 7, 1000)]), {"S": 0.0}, {"S": 1.0})
    t[0] += 5
    await eng.tick(snap([("S-1", 1, "Charging", 7, 6500)]), {"S": 0.0}, {"S": 1.0})
    assert ("stop", "S-1", 7) in eng.clients.simulator.calls
    assert eng.status()["S"]["completed"] == 1


async def test_impatient_drivers_leave_and_count_as_lost():
    eng, t, lost = engine()
    Car = __import__("control.demand", fromlist=["Car"]).Car
    eng.state["S"].queue.append(Car(1, "S", 8000, 0, 60, "TAG"))
    t[0] = 61
    texts = await eng.tick(snap([("S-1", 1, "Charging", 3, 0)]), {"S": 0.0}, {"S": 1.0})
    assert eng.status()["S"]["abandoned"] == 1 and lost == [("S", 8.0)]
    assert any("gave up" in t for t in texts)


async def test_interrupted_session_and_start_timeout():
    eng, t, lost = engine()
    Car = __import__("control.demand", fromlist=["Car"]).Car
    eng.state["S"].queue.append(Car(1, "S", 10000, 0, 9999, "TAG"))
    await eng.tick(snap([("S-1", 1, "Available", None, 0)]), {"S": 0.0}, {"S": 1.0})
    t[0] += START_TIMEOUT_S + 1
    await eng.tick(snap([("S-1", 1, "Available", None, 0)]), {"S": 0.0}, {"S": 1.0})
    assert len(eng.state["S"].queue) + len(eng.state["S"].sessions) == 1  # back in line, then re-assigned
    t[0] += 1
    await eng.tick(snap([("S-1", 1, "Charging", 9, 0)]), {"S": 0.0}, {"S": 1.0})
    t[0] += 1
    await eng.tick(snap([("S-1", 1, "Faulted", None, 2000)]), {"S": 0.0}, {"S": 1.0})
    assert eng.status()["S"]["interrupted"] == 1 and lost and lost[-1][1] == 8.0


async def test_failed_start_keeps_car_in_queue():
    eng, t, _ = engine()
    eng.clients.simulator.fail = True
    Car = __import__("control.demand", fromlist=["Car"]).Car
    eng.state["S"].queue.append(Car(1, "S", 1000, 0, 9999, "TAG"))
    await eng.tick(snap([("S-1", 1, "Available", None, 0)]), {"S": 0.0}, {"S": 1.0})
    assert len(eng.state["S"].queue) == 1 and not eng.state["S"].sessions


async def test_untracked_running_sessions_are_adopted_and_finish():
    eng, t, _ = engine()
    texts = await eng.tick(snap([("S-1", 1, "Charging", 461, 1000)]), {"S": 0.0}, {"S": 1.0})
    assert "took over 1 running session" in texts[0]
    t[0] += 5
    await eng.tick(snap([("S-1", 1, "Charging", 461, 100_000)]), {"S": 0.0}, {"S": 1.0})
    assert ("stop", "S-1", 461) in eng.clients.simulator.calls


async def test_session_info_gives_expected_finish_and_preparing_sessions_are_adopted():
    eng, t, _ = engine()
    await eng.tick(snap([("S-1", 1, "Preparing", 32, 0)]), {"S": 0.0}, {"S": 1.0})
    stations = {"S-1": {"id": "S-1", "connectors": [{"id": 1, "energy_wh": 2000, "power_w": 10_000}]}}
    info = eng.session_info(stations)["S-1#1"]
    assert info["adopted"] and info["delivered_kwh"] == 2.0
    assert abs(info["eta_s"] - (info["need_kwh"] * 1000 - 2000) / 10_000 * 3600) < 20  # need_kwh is rounded
