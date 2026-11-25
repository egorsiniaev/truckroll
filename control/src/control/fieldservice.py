"""Field service: a dispatched technician drives out, works on site and fixes the charger for real (in the
simulator). Times are compressed (a real truck roll takes 2-4 h) so the whole loop is visible on the page.
"""

from __future__ import annotations

import itertools
import logging
import random
import time
from dataclasses import dataclass, field

from control.clients import UpstreamError
from control.persist import Clocks

log = logging.getLogger(__name__)

DRIVE_S = (15 * 60, 30 * 60)
WORK_S = (10 * 60, 20 * 60)
VISIT_EUR = 150.0


@dataclass
class Job:
    id: str
    incident_id: str
    incident_key: str
    kind: str  # truck_roll | calibration
    station: str
    site: str
    dispatched_at: float
    arrive_at: float
    done_at: float
    status: str = "driving"  # driving -> on_site -> done
    finding: str = ""

    def to_json(self, clocks: Clocks | None = None) -> dict:
        d = dict(self.__dict__)
        if clocks is not None:
            for k in ("dispatched_at", "arrive_at", "done_at"):
                d[k] = clocks.to_wall(d[k])
        return d


@dataclass
class FieldService:
    clients: object
    clock: object = time.monotonic
    rng: random.Random = field(default_factory=random.Random)
    on_cost: object = None  # callable(site, eur)
    jobs: dict[str, Job] = field(default_factory=dict)
    _seq: itertools.count = field(default_factory=lambda: itertools.count(1))

    def dispatch(self, incident_id: str, incident_key: str, kind: str, station: str, site: str) -> Job:
        now = self.clock()
        arrive = now + self.rng.uniform(*DRIVE_S)
        job = Job(f"JOB-{next(self._seq):04d}", incident_id, incident_key, kind, station, site, now, arrive,
                  arrive + self.rng.uniform(*WORK_S))
        self.jobs[job.id] = job
        if self.on_cost:
            self.on_cost(site, VISIT_EUR)
        return job

    def active_for(self, incident_id: str) -> Job | None:
        return next((j for j in self.jobs.values() if j.incident_id == incident_id and j.status != "done"), None)

    async def tick(self, manager) -> None:
        now = self.clock()
        for job in [j for j in self.jobs.values() if j.status != "done"]:
            inc = manager.open.get(job.incident_key)
            if job.status == "driving" and now >= job.arrive_at:
                job.status = "on_site"
                if inc:
                    inc.add("update", f"Technician arrived at {job.station}.", "technician", short="on site")
            if job.status == "on_site" and now >= job.done_at:
                job.status = "done"
                job.finding = await self._fix(job, inc)
                if inc:
                    inc.add("response", job.finding, "technician", short="fixed on site")
                    inc.memo["field_fixed"] = True
                    if job.incident_key.startswith(("meter:", "jump:")):
                        inc.release_billing("Billing released: meter verified on site; held sessions re-billed from "
                                            "the verified meter.", "technician")
                        manager._close(job.incident_key, "Meter checked on site.", "technician", short="resolved")
        for jid in [j.id for j in self.jobs.values() if j.status == "done" and now - j.done_at > 3600]:
            self.jobs.pop(jid)

    async def _fix(self, job: Job, inc) -> str:
        """What a technician would do, applied to the simulated charger."""
        sid, csms, sim = job.station, self.clients.csms, self.clients.simulator
        kind = inc.kind if inc else ""
        try:
            if kind in ("fault", "stuck"):
                for cid in (1, 2):
                    try:
                        await sim.set_status(sid, cid, "Available", "NoError")
                    except UpstreamError:
                        pass
                return "Replaced the faulty part and tested a session: charger back in service."
            if kind == "station_offline":
                await sim.set_running(sid, True)
                return "Power-cycled the charger and replaced its modem: back online."
            if kind == "ignores_limit":
                chaos = await csms.chaos()
                await csms.chaos(drop_profiles=[s for s in chaos.get("drop_profiles", []) if s != sid])
                await csms.availability(sid, "Operative")
                return "Updated the charger firmware; it applies power limits again and is back in service."
            if kind in ("meter_drift", "meter_jump"):
                chaos = await csms.chaos()
                factors = chaos.get("meter_factor", {})
                if sid in factors:
                    await csms.chaos(meter_factor={k: v for k, v in factors.items() if k != sid})
                    return "Meter was reading wrong: replaced and verified."
                return "Meter verified on site: it measures correctly. Readings issue was elsewhere."
            return "Inspected the charger; no fault found."
        except UpstreamError as exc:
            return f"Technician could not finish: {exc}."

    # ---- survive restarts ---------------------------------------------------------------
    def to_state(self, clocks: Clocks) -> dict:
        return {"seq": next(self._seq), "jobs": [j.to_json(clocks) for j in self.jobs.values()]}

    def from_state(self, data: dict, clocks: Clocks) -> None:
        self.jobs = {}
        for d in data.get("jobs", []):
            for k in ("dispatched_at", "arrive_at", "done_at"):
                d[k] = clocks.to_mono(d[k])
            self.jobs[d["id"]] = Job(**d)
        self._seq = itertools.count(data.get("seq", 1))
