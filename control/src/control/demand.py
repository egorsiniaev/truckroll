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

log = logging.getLogger(__name__)

START_TIMEOUT_S = 40
WARM_START_HOURS = 0.5  # on start, seed each site with about half an hour of arrivals so the map isn't empty  # a requested session that never shows up on the CSMS goes back to the queue
DONE_SHARE = 0.9  # leaving with 90 % of what you wanted counts as a full session


@dataclass(frozen=True)
class Profile:
    cars_per_hour: float  # at a demand share of 1.0
    need_kwh: tuple[float, float]
    patience_min: tuple[float, float]


PROFILES: dict[str, Profile] = {
    "depot": Profile(14, (15, 40), (120, 240)),  # vans wait; they have all night
    "retail": Profile(30, (5, 15), (8, 20)),
    "highway": Profile(36, (20, 45), (6, 14)),
    "office": Profile(14, (8, 20), (30, 60)),
}


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


@dataclass
class SiteDemand:
    queue: deque = field(default_factory=deque)
    sessions: dict = field(default_factory=dict)  # (station, connector) -> Session
    completed: int = 0
    interrupted: int = 0
    abandoned: int = 0
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
            }
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
            arrivals = round(rate * WARM_START_HOURS) if not self._warm else self._arrivals(code, rate, dt)
            for _ in range(arrivals):
                st.queue.append(Car(next(self._ids), code, self.rng.uniform(*prof.need_kwh) * 1000, now,
                                    self.rng.uniform(*prof.patience_min) * 60, self.rng.choice(self.id_tags)))
            texts += await self._progress(code, st, stations, now)
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
                elif now - sess.requested > START_TIMEOUT_S:
                    del st.sessions[key]
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
                    log.info("stop %s failed: %s", sess.station, exc)
                    continue
                del st.sessions[key]
                st.completed += 1
        return texts

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
        while st.queue and free:
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
