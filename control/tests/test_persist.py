from control.demand import Car, DemandEngine, Session
from control.incidents import IncidentManager
from control.persist import Clocks

SITE = {"code": "S", "demand": "retail"}


def test_clocks_round_trip_and_downtime_counts_as_elapsed():
    before = Clocks(mono=1000.0, wall=50_000.0)
    w = before.to_wall(990.0)  # 10 s ago
    after = Clocks(mono=5.0, wall=50_100.0)  # restarted; 100 s later on the wall clock
    assert after.to_mono(w) == 5.0 - 110.0


def test_demand_queue_sessions_and_counters_survive():
    eng = DemandEngine([SITE], clients=None, id_tags=["T"], clock=lambda: 100.0)
    st = eng.state["S"]
    st.queue.append(Car(7, "S", 9000, 90.0, 600, "T"))
    st.sessions[("S-1", 1)] = Session(Car(8, "S", 5000, 80.0, 600, "T"), "S-1", 1, 85.0, 1200.0, 461)
    st.abandoned, st.diverted = 3, 4
    saved = eng.to_state(Clocks(mono=100.0, wall=10_000.0))

    again = DemandEngine([SITE], clients=None, id_tags=["T"])
    again.from_state(saved, Clocks(mono=20.0, wall=10_060.0))
    st2 = again.state["S"]
    assert [c.id for c in st2.queue] == [7] and st2.queue[0].arrived == 20.0 - 70.0  # waited 10 s + 60 s down
    assert st2.sessions[("S-1", 1)].tx == 461 and st2.sessions[("S-1", 1)].start_wh == 1200.0
    assert (st2.abandoned, st2.diverted) == (3, 4)


def test_open_incident_with_pending_timer_survives():
    m = IncidentManager([{"code": "S"}], clients=None, clock=lambda: 500.0)
    inc = m._new("fault:S-1", "fault", "critical", "S-1 reports X", "S", ["S-1"])
    inc.add("detected", "x", short="fault")
    inc.memo.update({"reset_at": 490.0, "truck": False})
    m._offline_since["S-2"] = 450.0
    saved = m.to_state(Clocks(mono=500.0, wall=1_000.0))

    again = IncidentManager([{"code": "S"}], clients=None, clock=lambda: 3.0)
    again.from_state(saved, Clocks(mono=3.0, wall=1_030.0))
    restored = again.open["fault:S-1"]
    assert restored.id == inc.id and restored.timeline[0]["short"] == "fault"
    assert restored.memo["reset_at"] == 3.0 - 40.0  # 10 s before save + 30 s down
    assert again._offline_since["S-2"] == 3.0 - 80.0
    assert again._new("x", "fault", "info", "t", "S", []).id != inc.id  # numbering continues



def test_untagged_timers_from_an_older_version_are_dropped_not_misread():
    m = IncidentManager([{"code": "S"}], clients=None, clock=lambda: 10.0)
    saved = {"seq": 2, "open": {"jump:S-1": {
        "id": "INC-0001", "kind": "meter_jump", "severity": "warning", "title": "t", "site": "S", "stations": ["S-1"],
        "opened": "2026-11-25T11:40:31Z", "opened_at": 1000.0, "timeline": [], "status": "open", "recovered": None,
        "ttr_s": None, "memo": {"last_jump": 98765.4, "jumps": [1.0], "reset_at": {"wall": 1005.0}}}},
        "closed": [], "offline_since": {}, "drops": {}, "prev_state": {}}
    m.from_state(saved, Clocks(mono=10.0, wall=1010.0))
    memo = m.open["jump:S-1"].memo
    assert "last_jump" not in memo and memo["reset_at"] == 5.0 and memo["jumps"] == [1.0]
