"""Incidents: what happened, how the system responded, and how it recovered.

Every response records its actor. Today all responses are automatic rules ("auto-rule"); the human-in-the-loop
step will add operator decisions with a deadline, falling back to the same rules when nobody answers.
"""

from __future__ import annotations

import itertools
import logging
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from control.clients import UpstreamError

log = logging.getLogger(__name__)

SITE_OUTAGE_SHARE = 0.5  # this share of a site's stations offline = one site incident, not N station incidents
SITE_RECOVERED_SHARE = 0.1
STATION_OFFLINE_AFTER_S = 30
TRUCK_ROLL_OFFLINE_S = 600
FAULT_RESET_AFTER_S = 45
TRUCK_ROLL_AFTER_RESET_S = 120
FLAP_COUNT, FLAP_WINDOW_S, FLAP_STABLE_S = 3, 180, 120
VERIFY_AFTER_FIX_S = 120  # after a remote fix, watch this long before calling it recovered
KEEP_CLOSED = 25


def _iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _dur(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m} min {s} s" if m else f"{s} s"


@dataclass
class Incident:
    id: str
    kind: str
    severity: str  # critical | warning | info
    title: str
    site: str
    stations: list[str]
    opened: str
    opened_at: float
    timeline: list[dict] = field(default_factory=list)
    status: str = "open"
    recovered: str | None = None
    ttr_s: float | None = None
    memo: dict = field(default_factory=dict)

    def add(self, kind: str, text: str, actor: str = "system") -> None:
        self.timeline.append({"time": _iso(), "type": kind, "actor": actor, "text": text})

    def to_json(self, now: float) -> dict:
        return {"id": self.id, "kind": self.kind, "severity": self.severity, "title": self.title, "site": self.site,
                "stations": self.stations, "opened": self.opened, "status": self.status,
                "recovered": self.recovered, "ttr_s": self.ttr_s, "age_s": round(now - self.opened_at),
                "timeline": self.timeline}


class IncidentManager:
    def __init__(self, sites: list[dict], clients, clock: Callable[[], float] = time.monotonic) -> None:
        self.sites = {s["code"]: s for s in sites}
        self.clients = clients
        self.clock = clock
        self.open: dict[str, Incident] = {}  # key -> incident
        self.closed: deque[Incident] = deque(maxlen=KEEP_CLOSED)
        self._seq = itertools.count(1)
        self._offline_since: dict[str, float] = {}
        self._drops: dict[str, deque[float]] = {}
        self._prev_state: dict[str, str] = {}

    def snapshot(self) -> list[dict]:
        now = self.clock()
        live = sorted(self.open.values(), key=lambda i: i.opened_at, reverse=True)
        return [i.to_json(now) for i in live] + [i.to_json(now) for i in self.closed]

    def _new(self, key: str, kind: str, severity: str, title: str, site: str, stations: list[str]) -> Incident:
        inc = Incident(f"INC-{next(self._seq):04d}", kind, severity, title, site, stations, _iso(), self.clock())
        self.open[key] = inc
        return inc

    def _close(self, key: str, text: str, actor: str = "system") -> None:
        inc = self.open.pop(key)
        inc.status, inc.recovered = "recovered", _iso()
        inc.ttr_s = round(self.clock() - inc.opened_at)
        inc.add("recovered", f"{text} Time to recover: {_dur(inc.ttr_s)}.", actor)
        self.closed.appendleft(inc)

    async def observe(self, snapshot: Mapping) -> None:
        now = self.clock()
        stations = snapshot.get("stations", [])
        self._track_offline(stations, now)
        outage_sites = self._site_outages(snapshot)
        self._crash_loops(stations, now)
        self._station_offline(stations, outage_sites, now)
        await self._faults(stations, now)

    # ---- offline tracking -------------------------------------------------
    def _track_offline(self, stations: list[dict], now: float) -> None:
        for st in stations:
            sid, state = st["id"], st["state"]
            if state == "offline":
                self._offline_since.setdefault(sid, now)
                if self._prev_state.get(sid, "offline") != "offline":
                    drops = self._drops.setdefault(sid, deque())
                    drops.append(now)
                    while drops and now - drops[0] > FLAP_WINDOW_S:
                        drops.popleft()
            else:
                self._offline_since.pop(sid, None)
            self._prev_state[sid] = state

    # ---- whole site cannot reach the backend --------------------------------
    def _site_outages(self, snapshot: Mapping) -> set[str]:
        affected = set()
        for row in snapshot.get("sites", []):
            code = row["code"]
            total = len(row.get("station_ids", []))
            offline = row.get("states", {}).get("offline", 0)
            share = offline / total if total else 0.0
            key = f"site-outage:{code}"
            if share >= SITE_OUTAGE_SHARE and total >= 2:
                affected.add(code)
                if key not in self.open:
                    inc = self._new(key, "site_outage", "critical", f"{code} lost contact with the backend", code, [])
                    inc.add("detected", f"{offline} of {total} stations went offline at once, so this is the site's "
                                        f"network, not the chargers.")
                    inc.add("response", "No remote fix possible while the site is unreachable. ISP ticket opened "
                                        "(simulated). Chargers keep running sessions locally and will report back "
                                        "when the line returns.", "auto-rule")
                    inc.memo["peak"] = offline
                else:
                    inc = self.open[key]
                    inc.memo["peak"] = max(inc.memo.get("peak", 0), offline)
            elif key in self.open and share <= SITE_RECOVERED_SHARE:
                peak = self.open[key].memo.get("peak", 0)
                self._close(key, f"All {peak} stations reconnected on their own; no truck roll needed.")
            elif key in self.open:
                affected.add(code)
        return affected

    # ---- station keeps rebooting -------------------------------------------
    def _crash_loops(self, stations: list[dict], now: float) -> None:
        by_site: dict[str, list[str]] = {}
        for st in stations:
            if len(self._drops.get(st["id"], ())) >= FLAP_COUNT:
                by_site.setdefault(st["site"], []).append(st["id"])
        for code, ids in by_site.items():
            key = f"crash-loop:{code}"
            if key not in self.open:
                inc = self._new(key, "crash_loop", "critical", f"Reboot loop at {code}", code, sorted(ids))
                inc.add("detected", f"{len(ids)} charger(s) dropped {FLAP_COUNT}+ times in {FLAP_WINDOW_S // 60} min: "
                                    f"{', '.join(sorted(ids))}. Pattern matches a bad firmware update.")
                inc.add("response", "Firmware rollback requested for these chargers (simulated). Automatic resets "
                                    "are paused for them, since resetting a crash loop only adds load.", "auto-rule")
            else:
                inc = self.open[key]
                new = sorted(set(ids) - set(inc.stations))
                if new:
                    inc.stations = sorted(set(inc.stations) | set(new))
                    inc.add("update", f"Also looping now: {', '.join(new)}.")
        for key in [k for k in self.open if k.startswith("crash-loop:")]:
            inc = self.open[key]
            stable = all(
                (not self._drops.get(sid) or now - self._drops[sid][-1] >= FLAP_STABLE_S)
                and sid not in self._offline_since
                for sid in inc.stations
            )
            if stable:
                for sid in inc.stations:
                    self._drops.pop(sid, None)
                self._close(key, f"Stable for {FLAP_STABLE_S // 60} min after the rollback.")

    def _looping(self, sid: str) -> bool:
        return any(sid in i.stations for k, i in self.open.items() if k.startswith("crash-loop:"))

    # ---- single station offline ----------------------------------------------
    def _station_offline(self, stations: list[dict], outage_sites: set[str], now: float) -> None:
        for st in stations:
            sid, key = st["id"], f"offline:{st['id']}"
            since = self._offline_since.get(sid)
            covered = st["site"] in outage_sites or self._looping(sid)
            if since is not None and not covered and now - since >= STATION_OFFLINE_AFTER_S and key not in self.open:
                inc = self._new(key, "station_offline", "warning", f"{sid} is offline", st["site"], [sid])
                inc.add("detected", f"No contact for {STATION_OFFLINE_AFTER_S} s while the rest of {st['site']} is fine.")
                inc.add("response", f"Waiting for it to reconnect. Truck roll if still offline after "
                                    f"{TRUCK_ROLL_OFFLINE_S // 60} min.", "auto-rule")
            if key in self.open:
                inc = self.open[key]
                if since is None:
                    self._close(key, "Station reconnected by itself.")
                elif covered:
                    self.open.pop(key)  # folded into the site/crash-loop incident
                elif now - since >= TRUCK_ROLL_OFFLINE_S and not inc.memo.get("truck"):
                    inc.memo["truck"] = True
                    inc.add("response", "Still offline: truck roll dispatched (simulated), technician ETA 2 h.",
                            "auto-rule")

    # ---- faults ------------------------------------------------------------------
    async def _faults(self, stations: list[dict], now: float) -> None:
        for st in stations:
            sid, key = st["id"], f"fault:{st['id']}"
            faulted = st["state"] == "faulted"
            if faulted and key not in self.open:
                codes = sorted({c["error_code"] for c in st["connectors"] if c["status"] == "Faulted"})
                count = sum(c["status"] == "Faulted" for c in st["connectors"])
                inc = self._new(key, "fault", "critical", f"{sid} reports {', '.join(codes)}", st["site"], [sid])
                inc.add("detected", f"{count} connector(s) faulted with {', '.join(codes)}.")
                inc.add("response", f"Remote soft reset in {FAULT_RESET_AFTER_S} s if it doesn't clear by itself.",
                        "auto-rule")
                continue
            if key not in self.open:
                continue
            inc = self.open[key]
            if not faulted:
                if inc.memo.get("truck"):
                    self._close(key, "Fixed on site by the technician.")
                elif inc.memo.get("reset_at") and not inc.memo.get("verify_from"):
                    inc.memo["verify_from"] = now
                    inc.status = "monitoring"
                    inc.add("update", f"Reset cleared the fault. Watching for {VERIFY_AFTER_FIX_S // 60} min before "
                                      f"closing.", "auto-rule")
                elif inc.memo.get("verify_from") and now - inc.memo["verify_from"] >= VERIFY_AFTER_FIX_S:
                    self._close(key, f"Healthy for {VERIFY_AFTER_FIX_S // 60} min after the remote reset; "
                                     f"no truck roll needed.", "auto-rule")
                elif not inc.memo.get("reset_at"):
                    self._close(key, "Fault cleared by itself.")
                continue
            if inc.memo.get("verify_from"):
                # Came back while we were watching: physical problem, resets won't help.
                inc.memo.pop("verify_from")
                inc.status = "open"
                inc.memo["truck"] = True
                inc.add("update", "Fault came back after the reset: this is hardware, not software.")
                inc.add("response", "Truck roll dispatched (simulated), technician ETA 2 h. No more resets; charger "
                                    "stays out of service until fixed.", "auto-rule")
                continue
            age = now - inc.opened_at
            if not inc.memo.get("reset_at") and not inc.memo.get("truck") and age >= FAULT_RESET_AFTER_S \
                    and st.get("connected"):
                inc.memo["reset_at"] = now
                try:
                    await self.clients.csms.reset(sid, "Soft")
                    inc.add("response", "Soft reset sent (OCPP Reset).", "auto-rule")
                except UpstreamError as exc:
                    inc.add("response", f"Soft reset failed: {exc}.", "auto-rule")
            elif inc.memo.get("reset_at") and not inc.memo.get("truck") \
                    and now - inc.memo["reset_at"] >= TRUCK_ROLL_AFTER_RESET_S:
                inc.memo["truck"] = True
                inc.add("response", "Reset didn't help: truck roll dispatched (simulated), technician ETA 2 h.",
                        "auto-rule")

    # ---- events from other parts of the system ---------------------------------------
    def grid_cap(self, ev: dict) -> None:
        key = f"grid-cap:{ev['site']}"
        kw = lambda w: f"{w / 1000:.0f} kW"  # noqa: E731
        if ev["kind"] == "limited" and key not in self.open:
            inc = self._new(key, "grid_cap", "warning", f"{ev['site']} over its grid cap", ev["site"], [])
            inc.add("detected", f"Site drew {kw(ev['demand_w'])}, above the {kw(ev['available_w'])} allowed "
                                f"(grid cap plus live solar).")
            inc.add("response", f"Site controller shares the cap: {ev['stations']} chargers limited to "
                                f"{kw(ev['share_w'])} each (OCPP smart charging).", "auto-rule")
        elif ev["kind"] == "reshared" and key in self.open:
            self.open[key].add("update", f"Cars came or left: share now {kw(ev['share_w'])} per charger.", "auto-rule")
        elif ev["kind"] == "released" and key in self.open:
            self._close(key, f"Demand fell to {kw(ev['demand_w'])}; limits lifted. Peak was {kw(ev['peak_w'])}.",
                        "auto-rule")

    def flex_request(self, site: str, active: bool, stations: int = 0, text: str = "") -> None:
        key = f"flex:{site}"
        if active and key not in self.open:
            inc = self._new(key, "flex_request", "info", f"Grid operator asked {site} to reduce load", site, [])
            inc.add("detected", text or "Congestion request received from the grid operator.")
            inc.add("response", f"Limits applied to {stations} chargers. Expected event, not a fault.", "auto-rule")
        elif not active and key in self.open:
            self._close(key, "Request ended; limits lifted.", "auto-rule")
