from control.fieldservice import DRIVE_S, WORK_S, FieldService
from control.incidents import IncidentManager


class Sim:
    def __init__(self):
        self.calls = []

    async def set_status(self, sid, cid, status, code):
        self.calls.append(("status", sid, cid, status))

    async def set_running(self, sid, on):
        self.calls.append(("running", sid, on))


class Csms:
    async def chaos(self, **kw):
        return {"meter_factor": {"S-1": 1.8}, "drop_profiles": []} if not kw else {}

    async def availability(self, sid, kind, connector_id=0):
        return {}


class Clients:
    def __init__(self):
        self.simulator, self.csms = Sim(), Csms()


async def test_truck_roll_drives_works_fixes_and_costs_money():
    t = [0.0]
    costs = []
    clients = Clients()
    m = IncidentManager([{"code": "S"}], clients, clock=lambda: t[0])
    fs = FieldService(clients, clock=lambda: t[0], on_cost=lambda site, eur: costs.append((site, eur)))
    m.field = fs
    inc = m._new("fault:S-1", "fault", "critical", "S-1 broken", "S", ["S-1"])
    await m._run(inc, "truck_roll", {"station": "S-1"}, "operator")
    assert costs == [("S", 150.0)] and "technician arrives about" in inc.timeline[-1]["text"]
    job = fs.active_for(inc.id)
    assert DRIVE_S[0] <= job.arrive_at <= DRIVE_S[1]
    assert m.snapshot()[0]["eta"]["basis"] == "technician on the way"
    t[0] = job.arrive_at
    await fs.tick(m)
    assert inc.timeline[-1]["short"] == "on site"
    t[0] = job.done_at
    await fs.tick(m)
    assert ("status", "S-1", 1, "Available") in clients.simulator.calls
    assert inc.timeline[-1]["actor"] == "technician" and inc.timeline[-1]["short"] == "fixed on site"


async def test_calibration_replaces_a_drifting_meter_and_closes():
    t = [0.0]
    m = IncidentManager([{"code": "S"}], Clients(), clock=lambda: t[0])
    fs = FieldService(m.clients, clock=lambda: t[0])
    m.field = fs
    inc = m._new("meter:S-1", "meter_drift", "warning", "meter", "S", ["S-1"])
    await m._run(inc, "calibration", {"station": "S-1"}, "auto-rule")
    t[0] = DRIVE_S[1] + WORK_S[1] + 1
    await fs.tick(m)
    await fs.tick(m)
    closed = m.snapshot()[0]
    assert closed["status"] == "recovered" and "replaced" in " ".join(e["text"] for e in closed["timeline"])
    assert closed["metrics"]["field_visit"] is True and closed["metrics"]["ttm_s"] is not None
