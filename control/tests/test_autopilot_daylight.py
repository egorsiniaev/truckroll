import random
from datetime import UTC, datetime

from control.autopilot import MAX_GAP_S, Autopilot
from control.daylight import daylight


class Runner:
    def __init__(self):
        self.current = None
        self.started = []

    def catalog(self):
        return [{"id": "a"}, {"id": "b"}]

    def start(self, sid, stations):
        self.started.append(sid)


def test_autopilot_waits_runs_and_never_repeats_back_to_back():
    t = [0.0]
    runner = Runner()
    ap = Autopilot(runner, enabled=True, clock=lambda: t[0], rng=random.Random(1))
    assert ap.tick([{"id": "x"}]) is None
    for _ in range(4):
        t[0] += MAX_GAP_S + 1
        ap.tick([{"id": "x"}])
    assert len(runner.started) == 4
    assert all(a != b for a, b in zip(runner.started, runner.started[1:], strict=False))


def test_autopilot_off_or_busy_does_nothing():
    t = [10_000.0]
    runner = Runner()
    ap = Autopilot(runner, enabled=False, clock=lambda: t[0])
    assert ap.tick([{"id": "x"}]) is None
    ap.set_enabled(True)
    runner.current = object()
    t[0] += MAX_GAP_S * 2
    assert ap.tick([{"id": "x"}]) is None and ap.status()["next_in_s"] is None


def at(hh, mm):
    return datetime(2026, 11, 24, hh - 1, mm, tzinfo=UTC)  # Amsterdam is UTC+1 in November


def test_phases_from_sun_times():
    rise, sset = "2026-11-24T08:12", "2026-11-24T16:38"
    assert daylight(at(6, 0), rise, sset)["phase"] == "night"
    assert daylight(at(9, 0), rise, sset)["phase"] == "morning"
    assert daylight(at(13, 0), rise, sset)["phase"] == "day"
    assert daylight(at(15, 30), rise, sset)["phase"] == "evening"
    assert daylight(at(18, 0), rise, sset)["phase"] == "night"
    assert daylight(at(13, 0), rise, sset)["local_time"] == "13:00"


def test_phases_without_sun_times_fall_back_to_clock():
    assert daylight(at(23, 0), None, None)["phase"] == "night"
