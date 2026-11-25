"""Demand engine: cars arrive, need energy, wait for a free connector, charge, and leave.

When a grid cap slows charging, sessions last longer and the queue grows. Drivers give up after their patience runs
out; that is lost revenue. Arrival rates follow the local day and, for public sites, the tariff.
"""

from __future__ import annotations

import itertools
import logging
import math
import random
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from control.clients import UpstreamError
from control.persist import Clocks

log = logging.getLogger(__name__)

START_TIMEOUT_S = 40
RECENT_S = 180
MAX_STARTS_PER_TICK = 4
MAX_STOP_ATTEMPTS = 3  # each start is a simulator round trip; keep one tick short
# A transaction can sit in any of these; a charger reporting the wrong one must not strand the session.
ADOPTABLE = {"Charging", "SuspendedEV", "SuspendedEVSE", "Preparing", "Finishing"}
MIN_POWER_FOR_ETA_W = 200
MASS_INTERRUPT = 3  # this many sessions cut at once on a site = the chargers restarted: refill like a warm start
WARM_START_HOURS = 0.5  # on start, seed each site with about half an hour of arrivals so the map isn't empty  # a requested session that never shows up on the CSMS goes back to the queue
DONE_SHARE = 0.9  # leaving with 90 % of what you wanted counts as a full session


@dataclass(frozen=True)
class Profile:
    cars_per_hour: float  # at a demand share of 1.0
    need_kwh: tuple[float, float]
    patience_min: tuple[float, float]
    session_h: float  # typical session length, to estimate the wait an app would show
    checks_app: bool  # public drivers look at the wait before coming; fleet vans and staff come anyway


PROFILES: dict[str, Profile] = {
    "depot": Profile(14, (15, 40), (120, 240), 2.0, False),  # vans wait; they have all night
    "retail": Profile(30, (5, 15), (8, 20), 0.5, True),
    "highway": Profile(36, (20, 45), (6, 14), 0.4, True),
    "office": Profile(14, (8, 20), (30, 60), 1.2, False),
}
MAX_DIVERT = 0.9


@dataclass
class Car:
    id: int
    site: str
    need_wh: float
    arrived: float
    patience_s: float
    id_tag: str


@dataclass
class Session:
    car: Car
    station: str
    connector: int
    requested: float
    start_wh: float | None = None
    tx: int | None = None
    stop_failures: int = 0


@dataclass
class SiteDemand:
    queue: deque = field(default_factory=deque)
    sessions: dict = field(default_factory=dict)  # (station, connector) -> Session
    completed: int = 0
    interrupted: int = 0
    abandoned: int = 0
    diverted: int = 0  # saw the wait in the app and went elsewhere
    starts_ok: int = 0
    starts_failed: int = 0
    recent_failures: deque = field(default_factory=deque)  # times of failed starts, for incident detection
    expected_wait_s: float = 0.0
    wait_s_total: float = 0.0
    served: int = 0


class DemandEngine:
    def __init__(self, sites: list[dict], clients, id_tags: list[str], rng: random.Random | None = None,
                 clock: Callable[[], float] = time.monotonic, on_lost: Callable[[str, float], None] | None = None) -> None:
        self.sites = {s["code"]: s for s in sites}
        self.clients = clients
        self.id_tags = id_tags or ["TR-0001"]
        self.rng = rng or random.Random()
        self.clock = clock
        self.on_lost = on_lost or (lambda site, kwh: None)
        self.state = {code: SiteDemand() for code in self.sites}
        self.rates: dict[str, float] = {}
        self._ids = itertools.count(1)
        self._last = self.clock()
        self._warm = False
        self._reseed: set[str] = set()  # sites emptied by a restart, refilled once their chargers are back

    def status(self) -> dict:
        out = {}
        for code, st in self.state.items():
            waits = [self.clock() - c.arrived for c in st.queue]
            out[code] = {
                "queue": len(st.queue), "charging_sessions": len(st.sessions),
                "longest_wait_s": round(max(waits)) if waits else 0,
                "avg_wait_s": round(st.wait_s_total / st.served) if st.served else 0,
                "arrivals_per_hour": round(self.rates.get(code, 0.0), 1),
                "completed": st.completed, "interrupted": st.interrupted, "abandoned": st.abandoned,
                "diverted": st.diverted, "expected_wait_s": round(st.expected_wait_s),
                "starts_ok": st.starts_ok, "starts_failed": st.starts_failed,
                "recent_start_failures": sum(1 for t in st.recent_failures if self.clock() - t <= RECENT_S),
            }
        return out

    # ---- survive restarts ---------------------------------------------------------------
    def to_state(self, clocks: Clocks) -> dict:
        def car(c: Car) -> dict:
            return {**c.__dict__, "arrived": clocks.to_wall(c.arrived)}

        return {"next_id": next(self._ids), "sites": {code: {
            "queue": [car(c) for c in st.queue],
            "sessions": [{"car": car(se.car), "station": se.station, "connector": se.connector,
                          "requested": clocks.to_wall(se.requested), "start_wh": se.start_wh, "tx": se.tx}
                         for se in st.sessions.values()],
            "counters": {k: getattr(st, k) for k in ("completed", "interrupted", "abandoned", "diverted",
                                                      "wait_s_total", "served", "starts_ok", "starts_failed")},
        } for code, st in self.state.items()}}

    def from_state(self, data: dict, clocks: Clocks) -> None:
        def car(d: dict) -> Car:
            return Car(**{**d, "arrived": clocks.to_mono(d["arrived"])})

        for code, saved in data.get("sites", {}).items():
            if code not in self.state:
                continue
            st = SiteDemand(queue=deque(car(c) for c in saved.get("queue", [])))
            for se in saved.get("sessions", []):
                st.sessions[(se["station"], se["connector"])] = Session(
                    car(se["car"]), se["station"], se["connector"], clocks.to_mono(se["requested"]), se["start_wh"], se["tx"])
            for k, v in saved.get("counters", {}).items():
                setattr(st, k, v)
            self.state[code] = st
        self._ids = itertools.count(data.get("next_id", 1))
        # The first tick tops each site up to a warm start, counting the restored queue and sessions.

    def session_info(self, stations: Mapping[str, Mapping]) -> dict[str, dict]:
        """Per "station#connector": what the car needs, what it has, and when it should be done at current power."""
        out = {}
        for st in self.state.values():
            for (sid, cid), sess in st.sessions.items():
                conn = next((c for c in stations.get(sid, {}).get("connectors", []) if c["id"] == cid), None)
                if conn is None:
                    continue
                delivered = max(0.0, (conn.get("energy_wh") or 0.0) - sess.start_wh) if sess.start_wh is not None else 0.0
                remaining = max(0.0, sess.car.need_wh - delivered)
                power = conn.get("power_w") or 0.0
                eta = remaining / power * 3600 if power >= MIN_POWER_FOR_ETA_W and sess.tx else None
                out[f"{sid}#{cid}"] = {"need_kwh": round(sess.car.need_wh / 1000, 1), "delivered_kwh": round(delivered / 1000, 1),
                                       "eta_s": round(eta) if eta is not None else None, "starting": sess.tx is None,
                                       "adopted": sess.car.id_tag == "adopted"}
        return out

    def _arrivals(self, code: str, rate_per_h: float, dt: float) -> int:
        lam = rate_per_h * dt / 3600
        # Poisson draw (Knuth); lam is small per tick.
        limit, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= self.rng.random()
            if p <= limit:
                return k
            k += 1

    async def tick(self, snapshot: Mapping, shares: Mapping[str, float], factors: Mapping[str, float]) -> list[str]:
        """shares: demand share of the hour per site; factors: tariff effect on arrivals. Returns feed texts."""
        now = self.clock()
        dt = max(0.0, min(30.0, now - self._last))
        self._last = now
        texts: list[str] = []
        stations = {s["id"]: s for s in snapshot.get("stations", [])}
        for code, site in self.sites.items():
            st = self.state[code]
            prof = PROFILES.get(site.get("demand", ""), PROFILES["retail"])
            rate = prof.cars_per_hour * shares.get(code, 0.6) * factors.get(code, 1.0)
            self.rates[code] = rate
            site_online = all(s.get("connected") for s in stations.values() if s["site"] == code)
            refill = code in self._reseed and site_online and not st.sessions
            if refill:
                self._reseed.discard(code)
                texts.append(f"{code}: chargers back after a restart; drivers return")
            warm = not self._warm or refill
            if warm:  # top up to half an hour of arrivals, counting cars already there
                arrivals = max(0, round(rate * WARM_START_HOURS) - len(st.queue) - len(st.sessions))
            else:
                arrivals = self._arrivals(code, rate, dt)
            connectors = sum(len(s.get("connectors", [])) for s in stations.values() if s["site"] == code) or 1
            # What the app shows: people ahead of you / how fast connectors free up.
            st.expected_wait_s = len(st.queue) / (connectors / prof.session_h) * 3600
            patience_s = sum(prof.patience_min) / 2 * 60
            divert = min(MAX_DIVERT, st.expected_wait_s / patience_s) if prof.checks_app and not warm else 0.0
            for _ in range(arrivals):
                need = self.rng.uniform(*prof.need_kwh) * 1000
                if divert and self.rng.random() < divert:
                    st.diverted += 1
                    self.on_lost(code, need / 1000)
                    continue
                st.queue.append(Car(next(self._ids), code, need, now,
                                    self.rng.uniform(*prof.patience_min) * 60, self.rng.choice(self.id_tags)))
            texts += self._adopt(code, st, prof, stations, now)
            cut_before = st.interrupted
            texts += await self._progress(code, st, stations, now)
            if st.interrupted - cut_before >= MASS_INTERRUPT and not st.sessions:
                self._reseed.add(code)
            texts += self._give_up(code, st, now)
            await self._assign(code, st, stations, now)
        self._warm = True
        return texts

    async def _progress(self, code: str, st: SiteDemand, stations: Mapping, now: float) -> list[str]:
        texts = []
        for key, sess in list(st.sessions.items()):
            station = stations.get(sess.station)
            conn = next((c for c in (station or {}).get("connectors", []) if c["id"] == sess.connector), None)
            if sess.tx is None:
                if conn and conn.get("transaction_id"):
                    sess.tx, sess.start_wh = conn["transaction_id"], conn.get("energy_wh") or 0.0
                    st.starts_ok += 1
                elif now - sess.requested > START_TIMEOUT_S:
                    del st.sessions[key]
                    st.starts_failed += 1
                    st.recent_failures.append(now)
                    while st.recent_failures and now - st.recent_failures[0] > RECENT_S:
                        st.recent_failures.popleft()
                    st.queue.appendleft(sess.car)  # try another connector
                continue
            if not station or not conn or conn.get("transaction_id") != sess.tx or not station.get("connected"):
                # Ended by something else: fault, reset, outage, scenario.
                del st.sessions[key]
                delivered = (conn.get("energy_wh", 0) - sess.start_wh) if conn else 0.0
                if delivered >= sess.car.need_wh * DONE_SHARE:
                    st.completed += 1
                else:
                    st.interrupted += 1
                    self.on_lost(code, max(0.0, sess.car.need_wh - delivered) / 1000)
                continue
            delivered = (conn.get("energy_wh") or 0.0) - (sess.start_wh or 0.0)
            if delivered >= sess.car.need_wh:
                try:
                    await self.clients.simulator.stop_transaction(sess.station, sess.tx)
                except UpstreamError as exc:
                    sess.stop_failures += 1
                    gone = "No active transaction" in str(exc)
                    if not gone and sess.stop_failures < MAX_STOP_ATTEMPTS:
                        log.info("stop %s failed: %s", sess.station, exc)
                        continue
                    # The session already ended on the charger, or keeps refusing: the car has left.
                    log.info("stop %s gave up after %d tries (%s)", sess.station, sess.stop_failures,
                             "no active transaction" if gone else "refused")
                del st.sessions[key]
                st.completed += 1
        return texts

    def _adopt(self, code: str, st: SiteDemand, prof: Profile, stations: Mapping, now: float) -> list[str]:
        """Running sessions we don't track (after a restart of this service or the CSMS): take them over and let
        them finish normally, so connectors are never stuck charging forever."""
        tracked = {s.tx for s in st.sessions.values()}
        adopted = 0
        for s in stations.values():
            if s["site"] != code or not s.get("connected"):
                continue
            for c in s.get("connectors", []):
                key = (s["id"], c["id"])
                tx = c.get("transaction_id")
                if tx and c["status"] in ADOPTABLE and tx not in tracked and key not in st.sessions:
                    car = Car(next(self._ids), code, self.rng.uniform(*prof.need_kwh) * 500, now, 0, "adopted")
                    st.sessions[key] = Session(car, s["id"], c["id"], now, start_wh=c.get("energy_wh") or 0.0, tx=tx)
                    adopted += 1
        return [f"{code}: took over {adopted} running session{'s' if adopted > 1 else ''} after a restart"] if adopted else []

    def _give_up(self, code: str, st: SiteDemand, now: float) -> list[str]:
        gone = [c for c in st.queue if now - c.arrived > c.patience_s]
        for car in gone:
            st.queue.remove(car)
            st.abandoned += 1
            self.on_lost(code, car.need_wh / 1000)
        if gone:
            return [f"{code}: {len(gone)} driver{'s' if len(gone) > 1 else ''} gave up waiting and left"]
        return []

    async def _assign(self, code: str, st: SiteDemand, stations: Mapping, now: float) -> None:
        busy = set(st.sessions)
        free = [
            (s["id"], c["id"])
            for s in sorted((x for x in stations.values() if x["site"] == code), key=lambda x: x["id"])
            if s.get("connected")
            for c in s.get("connectors", [])
            if c["status"] == "Available" and not c.get("transaction_id") and (s["id"], c["id"]) not in busy
        ]
        started = 0
        while st.queue and free and started < MAX_STARTS_PER_TICK:
            started += 1
            car = st.queue.popleft()
            station, connector = free.pop(0)
            try:
                await self.clients.simulator.start_transaction(station, connector, car.id_tag)
            except UpstreamError as exc:
                log.info("start on %s#%s failed: %s", station, connector, exc)
                st.queue.appendleft(car)
                break
            st.sessions[(station, connector)] = Session(car, station, connector, now)
            st.wait_s_total += now - car.arrived
            st.served += 1
