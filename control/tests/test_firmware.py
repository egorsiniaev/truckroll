import random
from datetime import datetime

from control.decisions import DecisionBook
from control.firmware import FIRST_VERSION, TZ, FirmwareManager, Install, bump_for, jump, next_version
from control.incidents import IncidentManager


class Csms:
    def __init__(self, answer="Accepted"):
        self.calls = []
        self.answer = answer

    async def availability(self, sid, kind, connector_id=0):
        self.calls.append((sid, kind))
        return {"status": self.answer if kind == "Inoperative" else "Accepted"}


class Clients:
    def __init__(self, answer="Accepted"):
        self.csms = Csms(answer)


def station(sid, site="S", status="Available", tx=None, connected=True):
    return {"id": sid, "site": site, "connected": connected,
            "connectors": [{"id": 1, "status": status, "transaction_id": tx}]}


def manager(when=datetime(2026, 11, 30, 11, 0, tzinfo=TZ), answer="Accepted", seed=1):
    t = [when.timestamp()]
    events = []
    fw = FirmwareManager([{"code": "S"}], Clients(answer), on_event=events.append, rng=random.Random(seed),
                         clock=lambda: t[0])
    return fw, t, events


def test_versions_bump_by_the_clock():
    assert bump_for(datetime(2026, 11, 30, 3, tzinfo=TZ)) == "major"  # Monday 03:00
    assert bump_for(datetime(2026, 12, 1, 3, tzinfo=TZ)) == "minor"  # other days at 03:00
    assert bump_for(datetime(2026, 12, 1, 15, tzinfo=TZ)) == "minor"
    assert bump_for(datetime(2026, 12, 1, 16, tzinfo=TZ)) == "patch"
    assert next_version("2.8.2", "patch") == "2.8.3"
    assert next_version("2.8.2", "minor") == "2.9.0"
    assert next_version("2.8.2", "major") == "3.0.0"


def test_install_length_follows_the_jump():
    assert jump("2.8.1", "2.8.4") == "patch"
    assert jump("2.6.3", "2.8.0") == "minor"
    assert jump("1.9.9", "2.0.1") == "major"


async def test_one_release_per_hour_starting_from_1_0_0():
    fw, t, events = manager()
    await fw.tick([])
    await fw.tick([])
    assert [r.version for r in fw.releases] == ["1.0.1"]
    t[0] += 3600
    await fw.tick([])
    assert [r.version for r in fw.releases] == ["1.0.1", "1.0.2"]
    assert fw.releases[0].status == "superseded"
    assert sum(e["type"] == "release" for e in events) == 2


async def test_only_free_chargers_update_and_a_quarter_at_a_time():
    fw, t, _ = manager()
    stations = [station(f"S-{n}") for n in range(1, 9)] + [station("S-9", status="Charging", tx=7),
                                                          station("S-10", connected=False)]
    await fw.tick(stations)
    assert len(fw.installs) == 2  # 25 % of 10
    assert "S-9" not in fw.installs and "S-10" not in fw.installs
    assert all(kind == "Inoperative" for _, kind in fw.clients.csms.calls)


async def test_charger_that_just_got_a_driver_is_left_alone():
    fw, _, _ = manager(answer="Scheduled")
    await fw.tick([station("S-1")])
    assert not fw.installs
    assert fw.clients.csms.calls == [("S-1", "Inoperative"), ("S-1", "Operative")]


async def test_install_completes_and_puts_the_charger_back():
    fw, t, events = manager()
    fw.rng = random.Random(4)
    await fw.tick([station("S-1")])
    inst = fw.installs["S-1"]
    inst.fails = False
    t[0] = inst.ends + 1
    await fw.tick([station("S-1", status="Unavailable")])
    assert fw.versions["S-1"] == "1.0.1" and "S-1" not in fw.installs
    assert ("S-1", "Operative") in fw.clients.csms.calls
    assert events[-1]["type"] == "installed"


async def test_failed_install_rolls_back_to_the_previous_version():
    fw, t, events = manager()
    await fw.tick([station("S-1")])
    fw.installs["S-1"].fails = True
    t[0] = fw.installs["S-1"].ends + 1
    await fw.tick([station("S-1", status="Unavailable")])
    assert fw.installs["S-1"].phase == "rolling_back"
    assert events[-1]["type"] == "install_failed"
    t[0] = fw.installs["S-1"].ends + 1
    await fw.tick([station("S-1", status="Unavailable")])
    assert fw.versions["S-1"] == FIRST_VERSION and "S-1" not in fw.installs
    assert events[-1]["type"] == "rolled_back"


async def test_failing_release_asks_to_halt_and_rolls_back_the_fleet():
    fw, t, events = manager()
    book = DecisionBook(enabled=True, clock=lambda: t[0])
    inc = IncidentManager([{"code": "S"}], fw.clients, clock=lambda: t[0], book=book)
    inc.firmware = fw
    fw.maybe_cut()
    target = fw.latest
    fw.versions = {"S-1": target, "S-2": FIRST_VERSION, "S-3": FIRST_VERSION}
    fw.previous = {"S-1": FIRST_VERSION}
    fw.releases[-1].installed = 1
    for sid in ("S-2", "S-3"):
        fw.installs[sid] = Install(sid, "S", FIRST_VERSION, target, "patch", t[0], t[0], True)
    await fw.tick([station("S-1"), station("S-2", status="Unavailable"), station("S-3", status="Unavailable")])
    for ev in events:
        await inc.firmware_event(ev)
    question = next(iter(book.pending.values()))
    assert question.action == "halt_release" and question.default == "approve"
    assert any(i.kind == "firmware_update" for i in inc.open.values())

    events.clear()
    t[0] = question.deadline_at + 1  # nobody answers: the default (halt) applies
    await inc.observe({"stations": [], "sites": []})
    assert fw.release(target).status == "halted"
    assert fw.pending_revert == {"S-1": FIRST_VERSION}
    for ev in events:
        await inc.firmware_event(ev)

    events.clear()
    t[0] += 200  # the two rollbacks finish, freeing the site's install slot
    await fw.tick([station("S-1"), station("S-2"), station("S-3")])  # S-1 is free, so it reverts now
    t[0] = fw.installs["S-1"].ends + 1
    await fw.tick([station("S-1", status="Unavailable"), station("S-2"), station("S-3")])
    assert fw.versions["S-1"] == FIRST_VERSION
    for ev in events:
        await inc.firmware_event(ev)
    assert not any(k.startswith("fw-release:") for k in inc.open)


async def test_state_survives_a_restart():
    fw, t, _ = manager()
    await fw.tick([station("S-1")])
    saved = fw.to_state()
    again, _, _ = manager()
    again.from_state(saved)
    assert again.versions == fw.versions and again.latest == fw.latest
    assert again.installs["S-1"].ends == fw.installs["S-1"].ends
    assert "flawed" not in again.status()["releases"][0]  # the hidden flaw never reaches the page


async def test_install_gives_way_to_a_driver_who_plugged_in_at_the_same_moment():
    fw, _, events = manager()
    await fw.tick([station("S-1")])
    assert "S-1" in fw.installs
    await fw.tick([station("S-1", status="Charging", tx=42)])
    assert "S-1" not in fw.installs and fw.versions["S-1"] == FIRST_VERSION
    assert events[-1]["type"] == "install_cancelled"
    assert fw.clients.csms.calls[-1] == ("S-1", "Operative")


async def test_charger_put_back_in_service_by_something_else_is_taken_out_again():
    fw, _, _ = manager()
    await fw.tick([station("S-1")])
    fw.clients.csms.calls.clear()
    await fw.tick([station("S-1")])  # still installing, but shows Available
    assert fw.clients.csms.calls == [("S-1", "Inoperative")]
