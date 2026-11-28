"""Firmware: a new release every hour, rolled out only to free chargers, with failed installs rolled back.

Versions are semantic. Patch every hour; minor every 6 hours (03, 09, 15, 21 local); major on Mondays at 03:00.
How long an install takes depends on the jump the charger makes: 2.8.1 -> 2.8.4 is quick, 2.6.x -> 2.8.x takes
longer, 1.x.x -> 2.x.x is a 20-30 min install. A charger is taken out of service (OCPP ChangeAvailability) while it
installs, so no driver can start on it. Some installs fail: the charger boots back into its previous image (A/B
slots), which is the rollback. When a release fails on too many chargers, the operator is asked to halt it and roll
back every charger already on it. Timers are wall-clock, so state survives restarts.
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from control.clients import UpstreamError

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Amsterdam")

FIRST_VERSION = "1.0.0"
MINOR_HOURS = {3, 9, 15, 21}
MAJOR_WEEKDAY, MAJOR_HOUR = 0, 3  # Monday 03:00, when the fewest drivers are around
INSTALL_S = {"patch": (60, 180), "minor": (300, 600), "major": (1200, 1800)}
FAIL_P = {"patch": 0.02, "minor": 0.06, "major": 0.12}
BAD_P = {"patch": 0.03, "minor": 0.08, "major": 0.15}  # chance a release has a flaw that fails most installs
BAD_FAIL_P = 0.5
ROLLBACK_S = (60, 180)  # boot back into the other slot and re-verify
SECURITY_P = 0.3
EXPLOITED_P = 0.03  # of security fixes: the flaw was already being exploited (CRA: report within 24 h)
SEVERITIES = (("low", 0.35), ("medium", 0.4), ("high", 0.2), ("critical", 0.05))
MAX_SHARE = 0.25  # at most a quarter of a site's chargers install at once, so the site keeps serving
HALT_MIN_FAILS, HALT_FAIL_SHARE = 2, 0.25
KEEP_RELEASES = 30
FAILURES = ("image signature check passed but boot verification timed out", "watchdog reset during first boot",
            "OCPP client did not reconnect after boot", "power module did not answer the self-test")
NOTES = {
    "patch": ("meter value rounding fix", "faster reconnect after network loss", "smaller status message bursts",
              "connector lock retry tuned", "log rotation fix"),
    "minor": ("smart charging profile stacking", "ISO 15118 plug-and-charge groundwork", "better thermal derating curve",
              "local authorisation list sync"),
    "major": ("new OCPP stack", "new bootloader and secure boot chain", "rewritten power module driver"),
}


def parse(v: str) -> tuple[int, int, int]:
    major, minor, patch = (int(x) for x in v.split("."))
    return major, minor, patch


def jump(current: str, target: str) -> str:
    """The size of the install a charger needs to get from current to target."""
    a, b = parse(current), parse(target)
    return "major" if a[0] != b[0] else "minor" if a[1] != b[1] else "patch"


def bump_for(local: datetime) -> str:
    if local.weekday() == MAJOR_WEEKDAY and local.hour == MAJOR_HOUR:
        return "major"
    return "minor" if local.hour in MINOR_HOURS else "patch"


def next_version(v: str, bump: str) -> str:
    major, minor, patch = parse(v)
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


@dataclass
class Release:
    version: str
    bump: str
    cut_at: float
    notes: str
    security: str | None = None  # severity of the vulnerability it fixes
    advisory: str | None = None
    exploited: bool = False
    flawed: bool = False  # hidden: makes most installs fail; operators only see the failures
    status: str = "rolling"  # rolling | superseded | halted
    installed: int = 0
    failed: int = 0
    reverted: int = 0

    def public(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "flawed"}


@dataclass
class Install:
    station: str
    site: str
    source: str  # version before
    target: str
    kind: str  # patch | minor | major | revert
    started: float
    ends: float
    fails: bool
    phase: str = "installing"  # installing | rolling_back | reverting
    reason: str = ""


@dataclass
class FirmwareManager:
    sites: list[dict]
    clients: object
    log_path: Path | None = None
    on_event: Callable[[dict], None] = lambda ev: None  # feed + incidents
    rng: random.Random = field(default_factory=random.Random)
    clock: Callable[[], float] = time.time
    versions: dict[str, str] = field(default_factory=dict)  # station -> running version
    previous: dict[str, str] = field(default_factory=dict)  # station -> version before its last update
    releases: list[Release] = field(default_factory=list)
    installs: dict[str, Install] = field(default_factory=dict)
    pending_revert: dict[str, str] = field(default_factory=dict)  # station on a halted release -> version to go back to
    last_slot: str = ""
    advisories: int = 0

    # ---- releases -------------------------------------------------------------------
    @property
    def latest(self) -> str:
        return self.releases[-1].version if self.releases else FIRST_VERSION

    def target(self) -> Release | None:
        """The newest release still allowed to roll out."""
        return next((r for r in reversed(self.releases) if r.status != "halted"), None)

    def release(self, version: str) -> Release | None:
        return next((r for r in self.releases if r.version == version), None)

    def maybe_cut(self) -> Release | None:
        local = datetime.fromtimestamp(self.clock(), TZ)
        slot = local.strftime("%Y-%m-%dT%H")
        if slot == self.last_slot:
            return None
        self.last_slot = slot
        bump = bump_for(local)
        rel = Release(next_version(self.latest, bump), bump, self.clock(), self.rng.choice(NOTES[bump]))
        if self.rng.random() < SECURITY_P:
            self.advisories += 1
            rel.security = self.rng.choices([s for s, _ in SEVERITIES], [w for _, w in SEVERITIES])[0]
            rel.advisory = f"TRSA-{local.year}-{self.advisories:03d}"
            rel.exploited = self.rng.random() < EXPLOITED_P
        rel.flawed = self.rng.random() < BAD_P[bump]
        for r in self.releases:
            if r.status == "rolling":
                r.status = "superseded"
        self.releases = [*self.releases, rel][-KEEP_RELEASES:]
        sec = f"; fixes {rel.advisory} ({rel.security}{', actively exploited' if rel.exploited else ''})" \
            if rel.security else ""
        self._emit({"type": "release", "version": rel.version, "bump": bump, "notes": rel.notes,
                    "security": rel.security, "advisory": rel.advisory, "exploited": rel.exploited,
                    "text": f"firmware {rel.version} released ({bump}: {rel.notes}{sec})"})
        return rel

    # ---- the loop -------------------------------------------------------------------
    async def tick(self, stations: list[Mapping]) -> None:
        for st in stations:
            self.versions.setdefault(st["id"], FIRST_VERSION)
        self.maybe_cut()
        by_id = {s["id"]: s for s in stations}
        await self._reconcile(by_id)
        for sid in [s for s, i in self.installs.items() if self.clock() >= i.ends]:
            await self._finish(self.installs[sid])
        await self._start_new(by_id)

    async def _reconcile(self, by_id: Mapping[str, Mapping]) -> None:
        """A driver who plugged in as the update began wins; anything else that put the charger back in service
        (a scenario clearing faults) is undone."""
        for sid, inst in list(self.installs.items()):
            conns = (by_id.get(sid) or {}).get("connectors", [])
            if not (by_id.get(sid) or {}).get("connected"):
                continue
            if inst.phase in ("installing", "reverting") and any(c.get("transaction_id") for c in conns):
                if await self._back_in(sid):
                    del self.installs[sid]  # a pending revert stays queued for when the charger is free
                    self._emit({"type": "install_cancelled", "station": sid, "site": inst.site, "to": inst.target,
                                "text": f"{sid}: update to {inst.target} postponed, a driver started charging"})
            elif any(c["status"] == "Available" for c in conns):
                await self._take_out(sid)

    def _free(self, st: Mapping) -> bool:
        conns = st.get("connectors", [])
        return bool(st.get("connected") and conns
                    and all(c["status"] == "Available" and not c.get("transaction_id") for c in conns))

    async def _start_new(self, by_id: Mapping[str, Mapping]) -> None:
        rel = self.target()
        for site in self.sites:
            mine = sorted((s for s in by_id.values() if s["site"] == site["code"]), key=lambda s: s["id"])
            room = max(1, int(len(mine) * MAX_SHARE)) - sum(1 for i in self.installs.values() if i.site == site["code"])
            for st in mine:
                if room <= 0:
                    break
                sid = st["id"]
                if sid in self.installs or not self._free(st):
                    continue
                if sid in self.pending_revert:
                    goal, kind = self.pending_revert[sid], "revert"
                elif rel is not None and self.versions[sid] != rel.version:
                    goal = rel.version
                    kind = jump(self.versions[sid], goal)
                else:
                    continue
                if await self._take_out(sid):
                    self._begin(sid, site["code"], goal, kind)
                    room -= 1

    async def _take_out(self, sid: str) -> bool:
        try:
            res = await self.clients.csms.availability(sid, "Inoperative")
        except UpstreamError as exc:
            log.info("firmware: could not take %s out of service: %s", sid, exc)
            return False
        answer = (res or {}).get("result", res or {}).get("status") if isinstance(res, dict) else None
        if answer not in (None, "Accepted"):
            # A driver plugged in between our look and the command: leave the charger to them.
            await self._back_in(sid)
            return False
        return True

    async def _back_in(self, sid: str) -> bool:
        try:
            await self.clients.csms.availability(sid, "Operative")
            return True
        except UpstreamError as exc:
            log.info("firmware: could not put %s back in service: %s", sid, exc)
            return False

    def _begin(self, sid: str, site: str, goal: str, kind: str) -> None:
        now = self.clock()
        source = self.versions[sid]
        if kind == "revert":
            inst = Install(sid, site, source, goal, kind, now, now + self.rng.uniform(*ROLLBACK_S), False, "reverting")
            self._emit({"type": "revert_started", "station": sid, "site": site, "from": source, "to": goal,
                        "text": f"{sid}: rolling back {source} -> {goal} (release halted)"})
        else:
            rel = self.release(goal)
            p = BAD_FAIL_P if rel and rel.flawed else FAIL_P[kind]
            inst = Install(sid, site, source, goal, kind, now, now + self.rng.uniform(*INSTALL_S[kind]),
                           self.rng.random() < p)
            self._emit({"type": "install_started", "station": sid, "site": site, "from": source, "to": goal,
                        "kind": kind, "minutes": round((inst.ends - now) / 60, 1),
                        "text": f"{sid}: installing {goal} ({kind}, about {round((inst.ends - now) / 60)} min)"})
        self.installs[sid] = inst

    async def _finish(self, inst: Install) -> None:
        sid = inst.station
        if inst.phase == "installing" and inst.fails:
            inst.phase, inst.reason = "rolling_back", self.rng.choice(FAILURES)
            inst.ends = self.clock() + self.rng.uniform(*ROLLBACK_S)
            rel = self.release(inst.target)
            if rel:
                rel.failed += 1
            self._emit({"type": "install_failed", "station": sid, "site": inst.site, "from": inst.source,
                        "to": inst.target, "kind": inst.kind, "reason": inst.reason,
                        "text": f"{sid}: firmware {inst.target} failed ({inst.reason}); rolling back to {inst.source}"})
            self._check_halt(rel)
            return
        if not await self._back_in(sid):
            return  # charger unreachable (outage): try again next tick
        del self.installs[sid]
        if inst.phase == "installing":
            self.previous[sid], self.versions[sid] = inst.source, inst.target
            rel = self.release(inst.target)
            if rel:
                rel.installed += 1
            self._emit({"type": "installed", "station": sid, "site": inst.site, "from": inst.source,
                        "to": inst.target, "kind": inst.kind, "minutes": round((self.clock() - inst.started) / 60, 1),
                        "text": f"{sid}: now on {inst.target}"})
        elif inst.phase == "rolling_back":
            self._emit({"type": "rolled_back", "station": sid, "site": inst.site, "from": inst.target,
                        "to": inst.source, "text": f"{sid}: rolled back to {inst.source}, back in service"})
        else:  # reverting off a halted release
            self.pending_revert.pop(sid, None)
            self.versions[sid] = inst.target
            rel = self.release(inst.source)
            if rel:
                rel.reverted += 1
            left = sum(1 for s in self.pending_revert if self.versions.get(s) == inst.source)
            self._emit({"type": "reverted", "station": sid, "site": inst.site, "from": inst.source,
                        "to": inst.target, "remaining": left, "text": f"{sid}: back on {inst.target}"})

    def _check_halt(self, rel: Release | None) -> None:
        if rel is None or rel.status == "halted":
            return
        tried = rel.installed + rel.failed
        if rel.failed >= HALT_MIN_FAILS and rel.failed / tried >= HALT_FAIL_SHARE:
            on_it = sorted(s for s, v in self.versions.items() if v == rel.version)
            self._emit({"type": "release_failing", "version": rel.version, "failed": rel.failed, "tried": tried,
                        "stations": on_it, "text": f"firmware {rel.version}: {rel.failed} of {tried} installs failed"})

    async def halt(self, version: str) -> dict:
        """Stop a release: no new installs, running ones roll back, chargers on it go back as soon as they're free."""
        rel = self.release(version)
        if rel is None or rel.status == "halted":
            return {"status": "Accepted"}
        rel.status = "halted"
        now = self.clock()
        for inst in self.installs.values():
            if inst.target == version and inst.phase == "installing":
                inst.phase, inst.fails, inst.reason = "rolling_back", True, "release halted"
                inst.ends = now + self.rng.uniform(*ROLLBACK_S)
        for sid, v in self.versions.items():
            if v == version:
                self.pending_revert[sid] = self.previous.get(sid, FIRST_VERSION)  # the image in the other slot
        self._emit({"type": "release_halted", "version": version, "stations": sorted(self.pending_revert),
                    "text": f"firmware {version} halted; {len(self.pending_revert)} chargers will roll back"})
        return {"status": "Accepted"}

    # ---- output ---------------------------------------------------------------------
    def _emit(self, ev: dict) -> None:
        ev = {"time": datetime.fromtimestamp(self.clock(), TZ).isoformat(timespec="seconds"), **ev}
        if self.log_path is not None:
            try:
                with self.log_path.open("a") as f:
                    f.write(json.dumps(ev) + "\n")
            except OSError as exc:
                log.warning("firmware log write failed: %s", exc)
        self.on_event(ev)

    def history(self, day: str) -> list[dict]:
        """Every firmware event on a local day (YYYY-MM-DD), from the log file."""
        if self.log_path is None or not self.log_path.exists():
            return []
        rows = []
        for line in self.log_path.read_text().splitlines():
            if line.startswith('{"time": "' + day):
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows

    def status(self) -> dict:
        now = self.clock()
        spread: dict[str, int] = {}
        for v in self.versions.values():
            spread[v] = spread.get(v, 0) + 1
        target = self.target()
        return {
            "latest": self.latest, "target": target.version if target else None,
            "on_target": spread.get(target.version, 0) if target else 0, "stations": len(self.versions),
            "versions": dict(sorted(spread.items(), key=lambda kv: parse(kv[0]), reverse=True)),
            "installing": [{"station": i.station, "site": i.site, "from": i.source, "to": i.target, "kind": i.kind,
                            "phase": i.phase, "left_s": max(0, round(i.ends - now))} for i in self.installs.values()],
            "releases": [r.public() for r in reversed(self.releases[-8:])],
            "pending_revert": len(self.pending_revert),
        }

    # ---- survive restarts (all timestamps are wall-clock) ------------------------------
    def to_state(self) -> dict:
        return {"versions": self.versions, "previous": self.previous, "last_slot": self.last_slot,
                "advisories": self.advisories, "pending_revert": self.pending_revert,
                "releases": [r.__dict__ for r in self.releases], "installs": [i.__dict__ for i in self.installs.values()]}

    def from_state(self, data: Mapping) -> None:
        self.versions = dict(data.get("versions", {}))
        self.previous = dict(data.get("previous", {}))
        self.last_slot = data.get("last_slot", "")
        self.advisories = data.get("advisories", 0)
        self.pending_revert = dict(data.get("pending_revert", {}))
        self.releases = [Release(**r) for r in data.get("releases", [])]
        self.installs = {i["station"]: Install(**i) for i in data.get("installs", [])}
