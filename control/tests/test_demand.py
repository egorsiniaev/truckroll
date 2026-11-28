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
                       on_lost=lambda site, kwh, why: lost.append((site, kwh)))
    eng._warm = True  # tests drive arrivals explicitly
    return eng, t, lost


async def test_warm_start_seeds_half_an_hour_of_arrivals():
    eng = DemandEngine([SITE], Clients(), ["TAG"], rng=random.Random(3), clock=lambda: 0.0)
    await eng.tick(snap([("S-1", 1, "Charging", 5, 0)]), {"S": 1.0}, {"S": 1.0})
    assert len(eng.state["S"].queue) == 15  # 30 cars/h x 0.5 h, all waiting: no free connector


async def test_site_refills_after_its_chargers_restart_and_come_back():
    eng, t, _ = engine()
    Session = __import__("control.demand", fromlist=["Session"]).Session
    Car = __import__("control.demand", fromlist=["Car"]).Car
    st = eng.state["S"]
    for cid in (1, 2, 3):
        st.sessions[("S-1", cid)] = Session(Car(cid, "S", 9e9, 0, 9e9, "TAG"), "S-1", cid, 0.0, 0.0, 100 + cid)
    offline = {"stations": [{"id": "S-1", "site": "S", "connected": False, "connectors": [
        {"id": c, "status": "Unavailable", "transaction_id": None, "energy_wh": 0} for c in (1, 2, 3)]}]}
    t[0] += 5
    await eng.tick(offline, {"S": 1.0}, {"S": 0.0})  # simulator restarting: all three sessions cut
    assert not st.sessions and st.interrupted == 3 and not st.queue
    t[0] += 5
    await eng.tick(offline, {"S": 1.0}, {"S": 1.0})  # still offline: no refill yet
    assert not st.queue
    t[0] += 5
    texts = await eng.tick(snap([("S-1", 1, "Available", None, 0)]), {"S": 1.0}, {"S": 1.0})
    assert any("drivers return" in x for x in texts)
    assert len(st.queue) + len(st.sessions) == 15  # half an hour of arrivals at 30 cars/h


async def test_first_tick_tops_up_a_restored_site_instead_of_adding_a_full_warm_start():
    eng = DemandEngine([SITE], Clients(), ["TAG"], rng=random.Random(3), clock=lambda: 0.0)
    Car = __import__("control.demand", fromlist=["Car"]).Car
    eng.state["S"].queue.extend(Car(i, "S", 5000, 0, 9e9, "TAG") for i in range(10))
    await eng.tick(snap([("S-1", 1, "Charging", 5, 0)]), {"S": 1.0}, {"S": 1.0})
    assert len(eng.state["S"].queue) == 15  # 10 restored + 5 to reach half an hour of arrivals


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
    await eng.tick(snap([("S-1", 1, "Charging", 9, 2000)]), {"S": 0.0}, {"S": 1.0})
    t[0] += 1
    await eng.tick(snap([("S-1", 1, "Faulted", None, 2000)]), {"S": 0.0}, {"S": 1.0})
    assert eng.status()["S"]["interrupted"] == 1 and lost and lost[-1][1] == 8.0


async def test_stale_register_from_the_previous_session_does_not_hide_delivered_energy():
    eng, t, _ = engine()
    Car = __import__("control.demand", fromlist=["Car"]).Car
    eng.state["S"].queue.append(Car(1, "S", 5000, 0, 9999, "TAG"))
    await eng.tick(snap([("S-1", 1, "Available", None, 30_000)]), {"S": 0.0}, {"S": 1.0})
    t[0] += 1
    # The new transaction appears while the backend still shows the last session's 30 kWh register.
    await eng.tick(snap([("S-1", 1, "Charging", 7, 30_000)]), {"S": 0.0}, {"S": 1.0})
    for wh in (1500, 3000, 5200):  # the register restarted at 0 for this session
        t[0] += 5
        await eng.tick(snap([("S-1", 1, "Charging", 7, wh)]), {"S": 0.0}, {"S": 1.0})
    assert ("stop", "S-1", 7) in eng.clients.simulator.calls  # 5 kWh reached: the car leaves


async def test_backend_session_energy_wins_over_the_register():
    Session = __import__("control.demand", fromlist=["Session"]).Session
    Car = __import__("control.demand", fromlist=["Car"]).Car
    delivered = __import__("control.demand", fromlist=["delivered"]).delivered
    sess = Session(Car(1, "S", 9e9, 0, 9e9, "T"), "S-1", 1, 0.0, start_wh=30_000.0, tx=7)
    assert delivered(sess, {"transaction_id": 7, "energy_wh": 4000, "session_wh": 4000}) == 4000
    assert delivered(sess, {"transaction_id": 8, "energy_wh": 0}) == 4000  # moved on: keep the last known


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
    stations = {"S-1": {"id": "S-1", "connectors": [{"id": 1, "transaction_id": 32, "energy_wh": 2000, "power_w": 10_000}]}}
    info = eng.session_info(stations)["S-1#1"]
    assert info["adopted"] and info["delivered_kwh"] == 2.0
    assert abs(info["eta_s"] - (info["need_kwh"] * 1000 - 2000) / 10_000 * 3600) < 20  # need_kwh is rounded


async def test_public_drivers_who_see_a_long_wait_in_the_app_go_elsewhere():
    eng, t, lost = engine()
    Car = __import__("control.demand", fromlist=["Car"]).Car
    for i in range(40):
        eng.state["S"].queue.append(Car(i, "S", 5000, 0, 99999, "TAG"))
    for _ in range(40):
        t[0] += 30
        await eng.tick(snap([("S-1", 1, "Charging", 5, 0)]), {"S": 1.0}, {"S": 1.0})
    st = eng.status()["S"]
    assert st["expected_wait_s"] > 3600 and st["diverted"] > 0 and lost


async def test_chargers_out_for_a_firmware_update_get_no_cars():
    eng = DemandEngine([SITE], Clients(), ["TAG"], rng=random.Random(3), clock=lambda: 0.0,
                       blocked=lambda: {"S-1"})
    await eng.tick(snap([("S-1", 1, "Available", None, 0), ("S-2", 1, "Available", None, 0)]), {"S": 1.0}, {"S": 1.0})
    assert [c[1] for c in eng.clients.simulator.calls] == ["S-2"]
