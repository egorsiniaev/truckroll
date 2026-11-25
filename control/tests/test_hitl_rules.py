from datetime import UTC, datetime

from control.decisions import DecisionBook
from control.incident_rules import AUTH_SLOW_S, DRIFT_MIN_S, OVER_LIMIT_S, RECOVER_AFTER_S
from control.incidents import IncidentManager
from control.slo import CAP_VIOLATION_S, SloTracker
from control.uptime import UptimeTracker


class Csms:
    def __init__(self):
        self.calls = []

    async def reset(self, sid, kind="Soft"):
        self.calls.append(("reset", sid))

    async def chaos(self, **kw):
        self.calls.append(("chaos", kw))

    async def availability(self, sid, kind, connector_id=0):
        self.calls.append(("availability", sid, kind))


class Limits:
    def __init__(self):
        self.reapplied = []

    async def reapply(self, sid):
        self.reapplied.append(sid)


class Clients:
    def __init__(self):
        self.csms = Csms()


def make(enabled=True):
    t = [0.0]
    book = DecisionBook(enabled=enabled, timeout_s=30, clock=lambda: t[0])
    limits = Limits()
    m = IncidentManager([{"code": "S", "power_w": 150_000}], Clients(), clock=lambda: t[0], book=book, limits=limits)
    return m, t, book, limits


def snap(backend=None, stations=(), sites=()):
    return {"backend": backend or {"auth_latency_s": 0, "reply_latency_s": 0}, "stations": list(stations), "sites": list(sites)}


async def test_question_times_out_to_the_default_and_says_so():
    m, t, book, _ = make()
    await m.observe(snap({"auth_latency_s": AUTH_SLOW_S, "reply_latency_s": 0}))
    [d] = book.snapshot()["pending"]
    assert d["action"] == "free_vend" and d["seconds_left"] == 45
    t[0] += 46
    await m.observe(snap({"auth_latency_s": AUTH_SLOW_S, "reply_latency_s": 0}))
    assert ("chaos", {"free_vend": True}) in m.clients.csms.calls
    inc = m.open["auth-slow"]
    assert inc.timeline[-1]["actor"] == "auto-rule" and "no answer in time" in inc.timeline[-1]["text"]


async def test_operator_can_say_no():
    m, t, book, _ = make()
    await m.observe(snap({"auth_latency_s": 0, "reply_latency_s": 9}))
    [d] = book.snapshot()["pending"]
    book.answer(d["id"], "reject")
    await m.observe(snap({"auth_latency_s": 0, "reply_latency_s": 9}))
    assert m.clients.csms.calls == []
    assert m.open["backlog"].timeline[-1]["short"] == "declined"


async def test_without_human_approval_everything_runs_at_once():
    m, t, book, _ = make(enabled=False)
    await m.observe(snap({"auth_latency_s": 0, "reply_latency_s": 9}))
    assert ("chaos", {"response_delay_s": 0}) in m.clients.csms.calls and book.snapshot()["pending"] == []


async def test_auth_recovery_turns_free_vend_off_and_closes():
    m, t, book, _ = make(enabled=False)
    await m.observe(snap({"auth_latency_s": 9, "reply_latency_s": 0}))
    for _ in range(3):
        t[0] += RECOVER_AFTER_S / 2
        await m.observe(snap({"auth_latency_s": 0.1, "reply_latency_s": 0}))
    assert ("chaos", {"free_vend": False}) in m.clients.csms.calls
    assert m.snapshot()[0]["status"] == "recovered"


def charging(sid, energy, power, tx=1):
    return {"id": sid, "site": "S", "connected": True, "state": "charging", "power_w": power, "limit_w": None,
            "connectors": [{"id": 1, "status": "Charging", "transaction_id": tx, "energy_wh": energy, "power_w": power,
                            "error_code": "NoError"}]}


async def test_meter_that_runs_fast_is_caught_and_honest_ones_are_not():
    m, t, book, _ = make(enabled=False)
    for step in range(0, DRIFT_MIN_S + 30, 10):
        t[0] = step
        honest = charging("S-1", energy=50_000 * step / 3600, power=50_000)
        fast = charging("S-2", energy=1.8 * 50_000 * step / 3600, power=50_000)
        noisy = charging("S-3", energy=1.3 * 50_000 * step / 3600, power=50_000)  # like the noisiest honest meter
        await m.observe(snap(stations=[honest, fast, noisy]))
    kinds = {k for k in m.open}
    assert kinds == {"meter:S-2"}
    shorts = [e["short"] for e in m.open["meter:S-2"].timeline]
    assert shorts[:3] == ["meter drift", "diagnostics", "billing hold"]
    assert "energy updates" in m.open["meter:S-2"].timeline[1]["text"]


async def test_charger_over_its_limit_gets_limit_resent_then_taken_out_of_service():
    m, t, book, limits = make(enabled=False)
    over = {**charging("S-1", 0, 60_000), "limit_w": 20_000}
    for step in range(0, OVER_LIMIT_S * 2 + 20, 10):
        t[0] = step
        await m.observe(snap(stations=[over]))
    assert limits.reapplied == ["S-1"]
    assert ("availability", "S-1", "Inoperative") in m.clients.csms.calls


def test_slo_counts_cap_episodes_and_flex_delivery():
    slo = SloTracker(["S"])
    row = {"code": "S", "power_w": 0, "cap_w": 100_000}
    for k in range(10):  # 100 s over the cap = one episode
        slo.accrue([{**row, "grid_w": 120_000}], {}, 10, k * 10, "d")
    for k in range(10):  # flex request: target 50 kW, half the time met
        slo.accrue([{**row, "grid_w": 40_000 if k % 2 else 80_000}], {"S": 50_000}, 10, 100 + k * 10, "d")
    by_id = {s["id"]: s for s in slo.summary({"fleet": 1.0, "fleet_excluding_injected": 1.0}, {"S": {"starts_ok": 49, "starts_failed": 1}})}
    assert by_id["cap"]["value"] == 1 and by_id["cap"]["status"] == "missed"
    assert by_id["flex"]["value"] == 0.5 and by_id["starts"]["value"] == 0.98 and by_id["starts"]["status"] == "met"


def test_uptime_excluding_injected_faults():
    up = UptimeTracker(["S"])
    now = datetime(2026, 11, 24, 12, tzinfo=UTC)
    down = {"id": "S-1", "site": "S", "connected": False, "connectors": [{"status": "Available"}]}
    upst = {"id": "S-2", "site": "S", "connected": True, "connectors": [{"status": "Available"}]}
    up.accrue([down, upst], 60, now, excluded_stations={"S-1"})
    s = up.summary()
    assert s["fleet"] == 0.5 and s["fleet_excluding_injected"] == 1.0


async def test_scheduled_answer_is_reported_honestly():
    m, t, book, _ = make(enabled=False)

    async def scheduled(sid, kind, connector_id=0):
        return {"station": sid, "status": "Scheduled"}

    m.clients.csms.availability = scheduled
    inc = m._new("k", "ignores_limit", "critical", "t", "S", ["S-1"])
    await m._run(inc, "inoperative", {"station": "S-1"}, "auto-rule")
    assert "when the current session ends" in inc.timeline[-1]["text"]
    assert inc.timeline[-1]["short"] == "out of service (scheduled)"


class TriggerCsms(Csms):
    async def trigger(self, sid, message="StatusNotification", connector_id=None):
        self.calls.append(("trigger", sid, message))


async def test_impossible_jump_is_its_own_incident_and_stays_out_of_the_drift_check():
    t = [0.0]
    book = DecisionBook(enabled=False, clock=lambda: t[0])
    clients = Clients()
    clients.csms = TriggerCsms()
    m = IncidentManager([{"code": "S", "power_w": 22_000}], clients, clock=lambda: t[0], book=book)
    energy = 0.0
    for step in range(0, 600, 30):
        t[0] = step
        energy += 22_000 * 30 / 3600 * 0.8  # honest 80 % of rated power
        if step in (300, 330, 360):
            energy += 14_000  # the simulator's impossible jump
        await m.observe(snap(stations=[charging("S-1", energy=energy, power=22_000 * 0.8)]))
    assert set(m.open) == {"jump:S-1"}  # no false drift incident
    inc = m.open["jump:S-1"]
    assert [e["short"] for e in inc.timeline][:2] == ["impossible reading", "billing hold"]
    assert ("trigger", "S-1", "MeterValues") in clients.csms.calls
    assert inc.memo["vendor_ticket"]  # third jump in an hour: vendor ticket (auto, approval off)
    assert inc.to_json(t[0])["billing"] == {"held_since": inc.memo["hold_since"], "released": None}
    for step in range(600, 600 + 1900, 30):  # 30 quiet minutes
        t[0] = step
        energy += 22_000 * 30 / 3600 * 0.8
        await m.observe(snap(stations=[charging("S-1", energy=energy, power=22_000 * 0.8)]))
    assert not m.open
    done = m.closed[0]
    assert [e["short"] for e in done.timeline][-2:] == ["billing released", "readings ok"]
    assert done.to_json(t[0])["billing"]["released"]



async def test_previous_sessions_reading_is_never_the_baseline_and_resets_are_not_drift():
    m, t, book, _ = make(enabled=False)
    # New session appears while the backend still shows the last session's 180 kWh, then the register restarts.
    readings = [180_000] * 3 + [50_000 * 10 * k / 3600 for k in range(1, 80)]  # 50 kW, 10 s steps
    for step, e in enumerate(readings):
        t[0] = step * 10
        await m.observe(snap(stations=[charging("S-1", energy=e, power=50_000)]))
    assert not [k for k in m.open if k.startswith("meter:")]
