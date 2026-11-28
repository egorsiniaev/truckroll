"""Incidents: what happened, how the system responded, and how it recovered.

Cheap, reversible responses run on their own ("auto-rule"). Risky ones (truck rolls, resets that interrupt a
driver, rollbacks, taking chargers out of service, free vend) become questions for an operator with a deadline;
no answer means the default applies. Every timeline entry records who acted.
"""

from __future__ import annotations

import itertools
import logging
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from control.clients import UpstreamError
from control.decisions import DecisionBook
from control.persist import Clocks

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Amsterdam")

SITE_OUTAGE_SHARE = 0.5  # this share of a site's stations offline = one site incident, not N station incidents
SITE_RECOVERED_SHARE = 0.1
STATION_OFFLINE_AFTER_S = 30
TRUCK_ROLL_OFFLINE_S = 600
FAULT_RESET_AFTER_S = 45
TRUCK_ROLL_AFTER_RESET_S = 120
FLAP_COUNT, FLAP_WINDOW_S, FLAP_STABLE_S = 3, 180, 120
VERIFY_AFTER_FIX_S = 120  # after a remote fix, watch this long before calling it recovered
STUCK_STATES = {"Preparing", "Finishing"}  # transitional: a connector should pass through them in about a minute
STUCK_AFTER_S = 180
STUCK_RESET_AFTER_S = 60  # after asking the charger to re-report its status
KEEP_CLOSED = 25
AGENT_AUTO_APPROVE_PCT = 70  # agent suggestions at or above this confidence default to yes
TIME_MEMOS = ("reset_at", "verify_from", "asked_at", "calm_since", "resent_at", "last_jump")  # monotonic stamps
# Responses that restore service or contain the impact, for time to mitigate.
MITIGATING = {"soft reset", "free vend", "worker restart", "out of service", "out of service (scheduled)",
              "chargers limited", "rollback", "limits applied", "re-sent limit", "billing hold", "fixed on site",
              "unstuck", "resets paused"}
TYPICAL_FIX_S = {"site_outage": 180, "station_offline": 600, "crash_loop": 400, "fault": 300, "stuck": 300,
                 "auth_slow": 480, "backlog": 180, "grid_cap": 1200, "flex_request": 200, "meter_drift": 1800,
                 "meter_jump": 1800, "ignores_limit": 600}


def _iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


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
    symptom_at: float | None = None  # when the problem started, as far as the data shows

    def add(self, kind: str, text: str, actor: str = "system", short: str | None = None) -> None:
        """short: two or three words for the one-line incident summary."""
        self.timeline.append({"time": _iso(), "type": kind, "actor": actor, "text": text, "short": short or kind})

    def hold_billing(self, text: str, actor: str = "auto-rule") -> None:
        if not self.memo.get("hold_since"):
            self.memo["hold_since"] = _iso()
            self.add("response", text, actor, short="billing hold")

    def release_billing(self, text: str, actor: str = "system") -> None:
        if self.memo.get("hold_since") and not self.memo.get("hold_released"):
            self.memo["hold_released"] = _iso()
            self.add("response", text, actor, short="billing released")

    def metrics(self) -> dict:
        """Time to detect / acknowledge / mitigate / resolve, in seconds from the problem's start or the alert."""
        opened = datetime.fromisoformat(self.opened.replace("Z", "+00:00"))

        def after(pred) -> float | None:
            for t in self.timeline:
                if pred(t):
                    return round((datetime.fromisoformat(t["time"].replace("Z", "+00:00")) - opened).total_seconds())
            return None

        return {
            "ttd_s": round(self.opened_at - self.symptom_at) if self.symptom_at is not None else None,
            "tta_s": after(lambda t: t["actor"] == "operator"),
            "ttm_s": after(lambda t: t["type"] == "response" and t["short"] in MITIGATING),
            "ttr_s": self.ttr_s,
            "field_visit": bool(self.memo.get("truck_roll") or self.memo.get("calibration")),
        }

    def to_json(self, now: float) -> dict:
        return {"id": self.id, "kind": self.kind, "severity": self.severity, "title": self.title, "site": self.site,
                "stations": self.stations, "opened": self.opened, "status": self.status,
                "recovered": self.recovered, "ttr_s": self.ttr_s,
                "age_s": round((datetime.now(UTC) - _parse(self.opened)).total_seconds()),
                "timeline": self.timeline, "metrics": self.metrics(), "eta": self.memo.get("eta"),
                "billing": {"held_since": self.memo.get("hold_since"), "released": self.memo.get("hold_released")}
                if self.memo.get("hold_since") else None}


class IncidentManager:
    def __init__(self, sites: list[dict], clients, clock: Callable[[], float] = time.monotonic,
                 book: DecisionBook | None = None, limits=None) -> None:
        self.sites = {s["code"]: s for s in sites}
        self.clients = clients
        self.clock = clock
        self.book = book or DecisionBook(enabled=False, clock=clock)
        self.limits = limits
        self.meters: dict = {}  # per connector: energy vs power cross-check (incident_rules.meter_drift)
        self.over_limit_since: dict[str, float] = {}  # station -> when it went above its limit
        self.meter_diag: dict[str, str] = {}  # station -> evidence for a drift incident
        self.field = None  # FieldService, set by the hub
        self.firmware = None  # FirmwareManager, set by the hub
        self.on_closed = None  # callable(incident json), e.g. to append to a history file
        self.open: dict[str, Incident] = {}  # key -> incident
        self.closed: deque[Incident] = deque(maxlen=KEEP_CLOSED)
        self._seq = itertools.count(1)
        self._offline_since: dict[str, float] = {}
        self._drops: dict[str, deque[float]] = {}
        self._prev_state: dict[str, str] = {}

    # ---- survive restarts ---------------------------------------------------------------
    def to_state(self, clocks: Clocks) -> dict:
        def inc(i: Incident) -> dict:
            # Timers are saved tagged as wall-clock time, so a later version never misreads them.
            memo = {k: ({"wall": clocks.to_wall(v)} if k in TIME_MEMOS and isinstance(v, (int, float)) else v)
                    for k, v in i.memo.items()}
            return {**i.__dict__, "opened_at": clocks.to_wall(i.opened_at), "symptom_at": clocks.to_wall(i.symptom_at),
                    "memo": memo}

        return {
            "seq": next(self._seq),
            "open": {k: inc(i) for k, i in self.open.items()},
            "closed": [inc(i) for i in self.closed],
            "offline_since": {k: clocks.to_wall(v) for k, v in self._offline_since.items()},
            "drops": {k: [clocks.to_wall(t) for t in v] for k, v in self._drops.items()},
            "prev_state": self._prev_state,
        }

    def from_state(self, data: dict, clocks: Clocks) -> None:
        def inc(d: dict) -> Incident:
            memo = {}
            for k, v in d.get("memo", {}).items():
                if isinstance(v, dict) and set(v) == {"wall"}:
                    memo[k] = clocks.to_mono(v["wall"])
                elif k in TIME_MEMOS and isinstance(v, (int, float)):
                    continue  # untagged timer from an older version: unreliable, so it restarts
                else:
                    memo[k] = v
            return Incident(**{**d, "opened_at": clocks.to_mono(d["opened_at"]),
                                "symptom_at": clocks.to_mono(d.get("symptom_at")), "memo": memo})

        self.open = {k: inc(d) for k, d in data.get("open", {}).items()}
        self.closed = deque((inc(d) for d in data.get("closed", [])), maxlen=KEEP_CLOSED)
        self._offline_since = {k: clocks.to_mono(v) for k, v in data.get("offline_since", {}).items()}
        self._drops = {k: deque(clocks.to_mono(t) for t in v) for k, v in data.get("drops", {}).items()}
        self._prev_state = dict(data.get("prev_state", {}))
        self._seq = itertools.count(data.get("seq", 1))

    def _estimate(self, inc: Incident, now: float) -> dict:
        """When will this be fixed, and why we think so."""
        job = self.field.active_for(inc.id) if self.field is not None else None
        if job is not None:
            basis = "technician on the way" if job.status == "driving" else "technician on site"
            return {"in_s": max(0, round(job.done_at - now)), "basis": basis}
        if any(d.incident_id == inc.id for d in self.book.pending.values()):
            return {"in_s": None, "basis": "waiting for your decision"}
        if inc.memo.get("verify_from") is not None:
            return {"in_s": max(0, round(inc.memo["verify_from"] + VERIFY_AFTER_FIX_S - now)), "basis": "watching after a fix"}
        done = [i.ttr_s for i in self.closed if i.kind == inc.kind and i.ttr_s]
        typical = sorted(done)[len(done) // 2] if len(done) >= 3 else TYPICAL_FIX_S.get(inc.kind, 600)
        basis = "median for this kind today" if len(done) >= 3 else "typical for this kind"
        return {"in_s": max(60, round(inc.opened_at + typical - now)), "basis": basis}

    def snapshot(self) -> list[dict]:
        now = self.clock()
        for inc in self.open.values():
            inc.memo["eta"] = self._estimate(inc, now)
        live = sorted(self.open.values(), key=lambda i: i.opened_at, reverse=True)
        return [i.to_json(now) for i in live] + [i.to_json(now) for i in self.closed]

    def _new(self, key: str, kind: str, severity: str, title: str, site: str, stations: list[str],
             symptom_age_s: float | None = None) -> Incident:
        now = self.clock()
        inc = Incident(f"INC-{next(self._seq):04d}", kind, severity, title, site, stations, _iso(), now)
        inc.symptom_at = now - symptom_age_s if symptom_age_s is not None else now
        self.open[key] = inc
        return inc

    def _close(self, key: str, text: str, actor: str = "system", short: str = "recovered") -> None:
        inc = self.open.pop(key)
        self.book.cancel_for(inc.id)
        inc.status, inc.recovered = "recovered", _iso()
        # From the stored wall-clock opening time: correct across restarts, unlike the process clock.
        inc.ttr_s = round((datetime.now(UTC) - _parse(inc.opened)).total_seconds())
        inc.add("recovered", f"{text} Time to recover: {_dur(inc.ttr_s)}.", actor, short)
        self.closed.appendleft(inc)
        if self.on_closed is not None:
            self.on_closed(inc.to_json(self.clock()))

    # ---- decisions: ask, then act on the answer ----------------------------------------
    async def _ask(self, inc: Incident, action: str, params: dict, question: str, why: str,
                   default: str = "approve", timeout_s: float | None = None) -> None:
        inc.memo.setdefault("asked", []).append(action)
        if not self.book.enabled:
            await self._run(inc, action, params, "auto-rule")
            return
        d = self.book.propose(inc.id, action, params, question, why, default, inc.site, inc.stations, timeout_s)
        wait = int(d.deadline_at - d.created_at)
        inc.add("decision", f"Asks an operator: {question} {why} No answer in {wait} s means "
                            f"{'yes' if default == 'approve' else 'no'}.", "system", short="asks you")

    def _asked(self, inc: Incident, action: str) -> bool:
        return action in inc.memo.get("asked", [])

    async def _resolve_decisions(self) -> None:
        self.book.expire()
        by_id = {i.id: i for i in self.open.values()}
        for d in self.book.resolved():
            inc = by_id.get(d.incident_id)
            if inc is not None:
                timed_out = d.decided_by == "timeout"
                actor = "auto-rule" if timed_out else "operator"
                late = timed_out and self.clock() - d.deadline_at > 30
                note = (" (no answer in time; ran late: the service was down at the deadline)" if late
                        else " (no answer in time)" if timed_out else " (approved)")
                if d.status == "approved":
                    await self._run(inc, d.action, d.params, actor, note)
                else:
                    inc.memo.setdefault("declined", []).append(d.action)
                    inc.add("update", f"{'No answer, default is no' if timed_out else 'Operator said no'}: "
                                      f"{d.question}", actor, short="declined")
            self.book.done(d)

    async def _run(self, inc: Incident, action: str, params: dict, actor: str, note: str = "") -> None:
        sid = params.get("station")
        steps = {
            "reset": (lambda: self.clients.csms.reset(sid, "Soft"), "Soft reset sent (OCPP Reset).", "soft reset"),
            "truck_roll": (None, "Truck roll dispatched (simulated).", "truck roll"),
            "rollback": (None, "Firmware rollback started on these chargers (simulated).", "rollback"),
            "free_vend": (lambda: self.clients.csms.chaos(free_vend=True),
                          "Free vend on: chargers accept every card without asking the auth backend.", "free vend"),
            "inoperative": (lambda: self.clients.csms.availability(sid, "Inoperative"),
                            "Charger taken out of service (OCPP ChangeAvailability) to protect the grid cap.",
                            "out of service"),
            "restart_worker": (lambda: self.clients.csms.chaos(response_delay_s=0),
                               "Restarted the stuck backend worker.", "worker restart"),
            "calibration": (None, "Meter calibration visit booked (simulated). Sessions stay on billing hold.",
                            "calibration"),
            "halt_release": (lambda: self.firmware.halt(params["version"]),
                             "Release halted: no new installs; chargers already on it roll back to their previous "
                             "image as soon as they are free.", "rollback"),
            "vendor_ticket": (None, "Firmware ticket opened with the charger vendor (simulated), with the impossible "
                                    "readings attached.", "vendor ticket"),
        }
        call, text, short = steps[action]
        try:
            res = await call() if call is not None else None
            answer = (res or {}).get("result", res or {}).get("status") if isinstance(res, dict) else None
            if answer == "Scheduled":
                # OCPP: a charger with a session running applies this when the session ends.
                text = text[:-1] + ", scheduled by the charger for when the current session ends."
                short = f"{short} (scheduled)"
            elif answer not in (None, "Accepted"):
                text = text[:-1] + f"; the charger answered {answer}."
            if action in ("truck_roll", "calibration") and self.field is not None and sid:
                key = next((k for k, v in self.open.items() if v is inc), None)
                job = self.field.dispatch(inc.id, key, action, sid, inc.site)
                eta = datetime.now(UTC) + timedelta(seconds=job.arrive_at - self.clock())
                text = text[:-1] + f"; technician arrives about {eta.astimezone(TZ).strftime('%H:%M')}."
            inc.add("response", text[:-1] + note + ".", actor, short=short)
            inc.memo[action] = True
            if action == "truck_roll":
                inc.memo["truck"] = True
            if action == "reset":
                inc.memo["reset_at"] = self.clock()
        except UpstreamError as exc:
            inc.add("response", f"{short} failed: {exc}.", actor, short=f"{short} failed")

    async def agent_verdict(self, incident_id: str, verdict: dict, action: str | None) -> None:
        """Write the agent's triage into the timeline; turn its suggestion into a question. A confident suggestion
        defaults to yes, an unsure one to no."""
        inc = next((i for i in self.open.values() if i.id == incident_id), None)
        if inc is None:
            return
        conf = round(float(verdict.get("confidence", 0)) * 100)
        clear = " It sees nothing to do." if verdict.get("all_clear") else ""
        inc.add("triage", f"Agent ({verdict['category']}, {conf}% sure): {verdict['summary']}{clear} "
                          f"Evidence: {verdict['evidence']}", "agent", short=f"agent: {verdict['category']}")
        inc.memo["agent_category"] = verdict["category"]
        if action and not self._asked(inc, action) and not inc.memo.get(action):
            params = {"station": inc.stations[0]} if inc.stations else {}
            if action in ("reset", "truck_roll", "inoperative", "calibration") and not inc.stations:
                return
            label = verdict["next_step"].replace("_", " ")
            # Confident: go ahead unless someone says no. Unsure: only with a yes.
            sure = conf >= AGENT_AUTO_APPROVE_PCT
            await self._ask(inc, action, params, f"Agent suggests: {label}. Do it?",
                            f"From the agent's triage ({conf}% sure)."
                            + (" It goes ahead unless you say no." if sure else " Below "
                               f"{AGENT_AUTO_APPROVE_PCT}% sure, so it needs a yes."),
                            default="approve" if sure else "reject", timeout_s=90)

    async def observe(self, snapshot: Mapping) -> None:
        from control import incident_rules

        now = self.clock()
        await self._resolve_decisions()
        stations = snapshot.get("stations", [])
        self._track_offline(stations, now)
        outage_sites = self._site_outages(snapshot)
        await self._crash_loops(stations, now)
        await self._station_offline(stations, outage_sites, now)
        await self._faults(stations, now)
        await self._stuck(stations, now)
        await incident_rules.auth_backend(self, snapshot)
        await incident_rules.backend_backlog(self, snapshot)
        await incident_rules.meter_drift(self, snapshot, now)
        await incident_rules.ignores_limit(self, snapshot, now)

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
                                        f"network, not the chargers.", short="site unreachable")
                    inc.add("response", "No remote fix possible while the site is unreachable. ISP ticket opened "
                                        "(simulated). Chargers keep running sessions locally and will report back "
                                        "when the line returns.", "auto-rule", short="ISP ticket")
                    inc.memo["peak"] = offline
                else:
                    inc = self.open[key]
                    inc.memo["peak"] = max(inc.memo.get("peak", 0), offline)
            elif key in self.open and share <= SITE_RECOVERED_SHARE:
                peak = self.open[key].memo.get("peak", 0)
                self._close(key, f"All {peak} stations reconnected on their own; no truck roll needed.", short="reconnected")
            elif key in self.open:
                affected.add(code)
        return affected

    # ---- station keeps rebooting -------------------------------------------
    async def _crash_loops(self, stations: list[dict], now: float) -> None:
        by_site: dict[str, list[str]] = {}
        for st in stations:
            if len(self._drops.get(st["id"], ())) >= FLAP_COUNT:
                by_site.setdefault(st["site"], []).append(st["id"])
        for code, ids in by_site.items():
            key = f"crash-loop:{code}"
            if key not in self.open:
                inc = self._new(key, "crash_loop", "critical", f"Reboot loop at {code}", code, sorted(ids))
                inc.add("detected", f"{len(ids)} charger(s) dropped {FLAP_COUNT}+ times in {FLAP_WINDOW_S // 60} min: "
                                    f"{', '.join(sorted(ids))}. Pattern matches a bad firmware update.", short="reboot loop")
                inc.add("response", "Automatic resets paused for these chargers: resetting a crash loop only adds "
                                    "load.", "auto-rule", short="resets paused")
                await self._ask(inc, "rollback", {"stations": sorted(ids)},
                                f"Roll back the firmware on {len(ids)} charger(s) at {code}?",
                                "Rollback should stop the loop but undoes the update on those chargers.")
            else:
                inc = self.open[key]
                new = sorted(set(ids) - set(inc.stations))
                if new:
                    inc.stations = sorted(set(inc.stations) | set(new))
                    inc.add("update", f"Also looping now: {', '.join(new)}.", short="more chargers")
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
                self._close(key, f"Stable for {FLAP_STABLE_S // 60} min after the rollback.", short="stable")

    def _looping(self, sid: str) -> bool:
        return any(sid in i.stations for k, i in self.open.items() if k.startswith("crash-loop:"))

    # ---- single station offline ----------------------------------------------
    async def _station_offline(self, stations: list[dict], outage_sites: set[str], now: float) -> None:
        for st in stations:
            sid, key = st["id"], f"offline:{st['id']}"
            since = self._offline_since.get(sid)
            covered = st["site"] in outage_sites or self._looping(sid)
            if since is not None and not covered and now - since >= STATION_OFFLINE_AFTER_S and key not in self.open:
                inc = self._new(key, "station_offline", "warning", f"{sid} is offline", st["site"], [sid],
                                symptom_age_s=now - since)
                inc.add("detected", f"No contact for {STATION_OFFLINE_AFTER_S} s while the rest of {st['site']} is fine.", short="no contact")
                inc.add("response", f"Waiting for it to reconnect. Truck roll if still offline after "
                                    f"{TRUCK_ROLL_OFFLINE_S // 60} min.", "auto-rule", short="waiting")
            if key in self.open:
                inc = self.open[key]
                if since is None:
                    self._close(key, "Station reconnected by itself.", short="reconnected")
                elif covered:
                    self.open.pop(key)  # folded into the site/crash-loop incident
                elif now - since >= TRUCK_ROLL_OFFLINE_S and not self._asked(inc, "truck_roll"):
                    await self._ask(inc, "truck_roll", {"station": sid}, f"Send a technician to {sid}?",
                                    f"Offline for {TRUCK_ROLL_OFFLINE_S // 60} min; a truck roll costs about €150.")

    # ---- faults ------------------------------------------------------------------
    async def _faults(self, stations: list[dict], now: float) -> None:
        for st in stations:
            sid, key = st["id"], f"fault:{st['id']}"
            faulted = st["state"] == "faulted"
            if faulted and key not in self.open:
                codes = sorted({c["error_code"] for c in st["connectors"] if c["status"] == "Faulted"})
                count = sum(c["status"] == "Faulted" for c in st["connectors"])
                real = [c for c in codes if c != "NoError"]
                label = ", ".join(real) if real else "a fault (no error code given)"
                age = max((c.get("status_age_s") or 0) for c in st["connectors"] if c["status"] == "Faulted")
                inc = self._new(key, "fault", "critical", f"{sid} reports {label}", st["site"], [sid], symptom_age_s=age)
                inc.add("detected", f"{count} connector(s) faulted with {', '.join(codes)}.", short="fault")
                inc.add("response", f"Remote soft reset in {FAULT_RESET_AFTER_S} s if it doesn't clear by itself.",
                        "auto-rule", short="reset planned")
                continue
            if key not in self.open:
                continue
            inc = self.open[key]
            if not faulted:
                if inc.memo.get("truck"):
                    job = self.field.active_for(inc.id) if self.field is not None else None
                    if job is None or inc.memo.get("field_fixed"):
                        self._close(key, "Technician finished on site; the charger is healthy.", "technician",
                                    short="resolved")
                    elif not inc.memo.get("cleared_before_visit"):
                        # Only the technician can say the cause is gone: keep the visit, watch until they're done.
                        inc.memo["cleared_before_visit"] = True
                        inc.status = "monitoring"
                        inc.add("update", "Fault cleared before the technician arrived; the visit goes ahead to "
                                          "check the cause.", short="cleared, visit kept")
                elif inc.memo.get("reset_at") and not inc.memo.get("verify_from"):
                    inc.memo["verify_from"] = now
                    inc.status = "monitoring"
                    inc.add("update", f"Reset cleared the fault. Watching for {VERIFY_AFTER_FIX_S // 60} min before "
                                      f"closing.", "auto-rule", short="watching")
                elif inc.memo.get("verify_from") and now - inc.memo["verify_from"] >= VERIFY_AFTER_FIX_S:
                    self._close(key, f"Healthy for {VERIFY_AFTER_FIX_S // 60} min after the remote reset; "
                                     f"no truck roll needed.", "auto-rule", short="fixed remotely")
                elif not inc.memo.get("reset_at"):
                    self._close(key, "Fault cleared by itself.", short="cleared")
                continue
            if inc.memo.pop("cleared_before_visit", None):
                inc.status = "open"
                inc.add("update", "Faulted again while the technician is on the way.", short="fault back")
                continue
            if inc.memo.get("verify_from"):
                # Came back while we were watching: physical problem, resets won't help.
                inc.memo.pop("verify_from")
                inc.status = "open"
                inc.add("update", "Fault came back after the reset: this is hardware, not software.", short="fault back")
                if not self._asked(inc, "truck_roll"):
                    await self._ask(inc, "truck_roll", {"station": sid}, f"Send a technician to {sid}?",
                                    "The fault returned after a remote reset, so it needs hands on site (about €150).")
                continue
            age = now - inc.opened_at
            if not inc.memo.get("reset_at") and not self._asked(inc, "truck_roll") and age >= FAULT_RESET_AFTER_S \
                    and st.get("connected"):
                await self._run(inc, "reset", {"station": sid}, "auto-rule")
            elif inc.memo.get("reset_at") and not self._asked(inc, "truck_roll") \
                    and now - inc.memo["reset_at"] >= TRUCK_ROLL_AFTER_RESET_S:
                await self._ask(inc, "truck_roll", {"station": sid}, f"Send a technician to {sid}?",
                                "A remote reset didn't clear the fault (about €150 per visit).")

    # ---- connector stuck in a transitional status ----------------------------------------
    async def _stuck(self, stations: list[dict], now: float) -> None:
        for st in stations:
            sid = st["id"]
            stuck = [c for c in st.get("connectors", []) if st.get("connected") and c["status"] in STUCK_STATES
                     and (c.get("status_age_s") or 0) >= STUCK_AFTER_S]
            key = f"stuck:{sid}"
            if stuck and key not in self.open and f"fault:{sid}" not in self.open and not self._looping(sid):
                c = stuck[0]
                flowing = (c.get("power_w") or 0) > 100
                inc = self._new(key, "stuck", "warning", f"{sid} stuck in {c['status']}", st["site"], [sid],
                                symptom_age_s=c.get("status_age_s"))
                inc.memo["connector"] = c["id"]
                detail = (" while energy is flowing: the charger's status and its meter disagree"
                          if flowing else ": no session is starting, the connector is blocked")
                inc.add("detected", f"Connector {c['id']} has been {c['status']} for {_dur(c['status_age_s'])}{detail}.",
                        short=f"stuck {c['status']}")
                try:
                    await self.clients.csms.trigger(sid, "StatusNotification", c["id"])
                    inc.add("response", "Asked the charger to re-send its status (OCPP TriggerMessage).", "auto-rule",
                            short="ask status")
                except UpstreamError as exc:
                    inc.add("response", f"Status request failed: {exc}.", "auto-rule", short="ask failed")
                inc.memo["asked_at"] = now
                continue
            if key not in self.open:
                continue
            inc = self.open[key]
            if not stuck:
                if inc.memo.get("verify_from") is None and inc.memo.get("reset_at"):
                    inc.memo["verify_from"] = now
                    inc.status = "monitoring"
                    inc.add("update", "Connector moved on after the reset. Watching before closing.", "auto-rule",
                            short="watching")
                elif inc.memo.get("verify_from") is not None:
                    if now - inc.memo["verify_from"] >= VERIFY_AFTER_FIX_S:
                        self._close(key, "Connector stayed healthy after the reset.", "auto-rule", short="fixed remotely")
                else:
                    self._close(key, "Connector moved on after the status request.", "auto-rule", short="unstuck")
                continue
            if inc.memo.get("verify_from") is not None:
                inc.memo.pop("verify_from")
                inc.status = "open"
                inc.add("update", "Stuck again after the reset.", short="stuck again")
                if not self._asked(inc, "truck_roll"):
                    await self._ask(inc, "truck_roll", {"station": sid}, f"Send a technician to {sid}?",
                                    "The connector gets stuck again after a reset (about €150 per visit).")
            elif not inc.memo.get("reset_at") and not self._asked(inc, "reset") \
                    and now - inc.memo.get("asked_at", now) >= STUCK_RESET_AFTER_S:
                if any((c.get("power_w") or 0) > 100 for c in stuck):
                    await self._ask(inc, "reset", {"station": sid}, f"Reset {sid} while it is still charging?",
                                    "Its status is stuck, but energy is flowing: a reset ends the driver's session.")
                else:
                    await self._run(inc, "reset", {"station": sid}, "auto-rule")

    # ---- events from other parts of the system ---------------------------------------
    def grid_cap(self, ev: dict) -> None:
        key = f"grid-cap:{ev['site']}"
        kw = lambda w: f"{w / 1000:.0f} kW"  # noqa: E731
        if ev["kind"] == "limited" and key not in self.open:
            inc = self._new(key, "grid_cap", "warning", f"{ev['site']} over its grid cap", ev["site"], [])
            inc.add("detected", f"Site drew {kw(ev['demand_w'])}, above the {kw(ev['available_w'])} allowed "
                                f"(grid cap plus live solar).", short="over cap")
            inc.add("response", f"Site controller shares the cap: {ev['stations']} chargers limited to "
                                f"{kw(ev['share_w'])} each (OCPP smart charging).", "auto-rule", short="chargers limited")
        elif ev["kind"] == "reshared" and key in self.open:
            self.open[key].add("update", f"Cars came or left: share now {kw(ev['share_w'])} per charger.", "auto-rule", short="re-shared")
        elif ev["kind"] == "released" and key in self.open:
            self._close(key, f"Demand fell to {kw(ev['demand_w'])}; limits lifted. Peak was {kw(ev['peak_w'])}.",
                        "auto-rule", short="limits lifted")

    async def firmware_event(self, ev: dict) -> None:
        """Failed installs and failing releases from the firmware rollout. Cause is known: our own update."""
        kind = ev["type"]
        if kind == "install_failed":
            key = f"fw:{ev['station']}"
            if key in self.open:
                return
            inc = self._new(key, "firmware_update", "warning", f"{ev['station']}: firmware {ev['to']} install failed",
                            ev["site"], [ev["station"]])
            inc.add("detected", f"Update {ev['from']} -> {ev['to']} ({ev['kind']}) failed: {ev['reason']}.",
                    short="update failed")
            inc.add("response", f"Automatic rollback to {ev['from']}: the charger boots its previous image and stays "
                                f"out of service until it checks out.", "auto-rule", short="rollback")
        elif kind == "rolled_back" and (key := f"fw:{ev['station']}") in self.open:
            self._close(key, f"Back on {ev['to']} and in service.", "auto-rule", short="rolled back")
        elif kind == "release_failing":
            key = f"fw-release:{ev['version']}"
            if key in self.open:
                self.open[key].add("update", f"{ev['failed']} of {ev['tried']} installs failed so far.", short="more fails")
                return
            inc = self._new(key, "firmware_release", "critical", f"Firmware {ev['version']} failing on chargers", "ALL",
                            ev["stations"])
            inc.add("detected", f"{ev['failed']} of {ev['tried']} installs of {ev['version']} failed; "
                                f"{len(ev['stations'])} chargers already run it.", short="bad release")
            await self._ask(inc, "halt_release", {"version": ev["version"]}, f"Halt firmware {ev['version']} and roll "
                            f"back the {len(ev['stations'])} chargers on it?",
                            "Each rollback takes a free charger out of service for 1-3 min.", default="approve",
                            timeout_s=90)
        elif kind == "release_halted" and (key := f"fw-release:{ev['version']}") in self.open:
            if not ev["stations"]:
                self._close(key, "Halted; no charger was left on it.", "auto-rule", short="halted")
            else:
                self.open[key].status = "monitoring"
                self.open[key].add("update", f"{len(ev['stations'])} chargers roll back when free.", short="rolling back")
        elif kind == "reverted" and (key := f"fw-release:{ev['from']}") in self.open and not ev.get("remaining"):
            self._close(key, f"Every charger is off {ev['from']}.", "auto-rule", short="rolled back")
        elif kind == "release":
            for key in [k for k, i in self.open.items() if k.startswith("fw-release:")
                        and "halt_release" in i.memo.get("declined", [])]:
                self._close(key, f"Kept running by choice; superseded by {ev['version']}.", "operator",
                            short="superseded")

    def flex_request(self, site: str, active: bool, stations: int = 0, text: str = "") -> None:
        key = f"flex:{site}"
        if active and key not in self.open:
            inc = self._new(key, "flex_request", "info", f"Grid operator asked {site} to reduce load", site, [])
            inc.add("detected", text or "Congestion request received from the grid operator.", short="grid request")
            inc.add("response", f"Limits applied to {stations} chargers. Expected event, not a fault.", "auto-rule", short="limits applied")
        elif not active and key in self.open:
            self._close(key, "Request ended; limits lifted.", "auto-rule", short="request ended")
