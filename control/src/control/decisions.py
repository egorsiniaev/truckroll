"""Human in the loop: risky responses become questions with a deadline. No answer in time means the default applies.

The incident engine proposes, the operator answers on the map (or the deadline passes), and the engine executes on
its next poll. Every outcome is written to the incident timeline with who decided.
"""

from __future__ import annotations

import itertools
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from control.persist import Clocks

DEFAULT_TIMEOUT_S = 60
KEEP_HISTORY = 30


@dataclass
class Decision:
    id: str
    incident_id: str
    action: str
    params: dict
    question: str
    why: str
    default: str  # "approve" or "reject": what happens if nobody answers
    created_at: float
    deadline_at: float
    site: str
    stations: list[str]
    status: str = "pending"  # pending -> approved | rejected
    decided_by: str | None = None  # "operator" or "timeout"
    executed: bool = False

    def to_json(self, now: float) -> dict:
        return {"id": self.id, "incident_id": self.incident_id, "action": self.action, "question": self.question,
                "why": self.why, "default": self.default, "site": self.site, "stations": self.stations,
                "status": self.status, "decided_by": self.decided_by,
                "seconds_left": max(0, round(self.deadline_at - now)) if self.status == "pending" else 0}


@dataclass
class DecisionBook:
    enabled: bool = True
    timeout_s: float = DEFAULT_TIMEOUT_S
    clock: Callable[[], float] = time.monotonic
    pending: dict[str, Decision] = field(default_factory=dict)
    history: deque = field(default_factory=lambda: deque(maxlen=KEEP_HISTORY))
    _seq: itertools.count = field(default_factory=lambda: itertools.count(1))

    def propose(self, incident_id: str, action: str, params: dict, question: str, why: str, default: str,
                site: str, stations: list[str], timeout_s: float | None = None) -> Decision:
        now = self.clock()
        d = Decision(f"DEC-{next(self._seq):04d}", incident_id, action, params, question, why, default, now,
                     now + (timeout_s or self.timeout_s), site, stations)
        self.pending[d.id] = d
        return d

    def answer(self, decision_id: str, choice: str) -> Decision:
        d = self.pending.get(decision_id)
        if d is None:
            raise KeyError(decision_id)
        if choice not in ("approve", "reject"):
            raise ValueError(choice)
        d.status, d.decided_by = ("approved" if choice == "approve" else "rejected"), "operator"
        return d

    def expire(self) -> None:
        now = self.clock()
        for d in self.pending.values():
            if d.status == "pending" and now >= d.deadline_at:
                d.status, d.decided_by = ("approved" if d.default == "approve" else "rejected"), "timeout"

    def resolved(self) -> list[Decision]:
        """Answered or timed out, not yet executed. The caller executes and then calls done()."""
        return [d for d in self.pending.values() if d.status != "pending" and not d.executed]

    def done(self, d: Decision) -> None:
        d.executed = True
        self.pending.pop(d.id, None)
        self.history.appendleft(d)

    def cancel_for(self, incident_id: str) -> None:
        """The incident resolved on its own; its open questions no longer matter."""
        for d in [d for d in self.pending.values() if d.incident_id == incident_id and d.status == "pending"]:
            d.status, d.decided_by, d.executed = "rejected", "obsolete", True
            self.pending.pop(d.id, None)
            self.history.appendleft(d)

    def snapshot(self) -> dict:
        now = self.clock()
        return {"enabled": self.enabled, "timeout_s": self.timeout_s,
                "pending": [d.to_json(now) for d in self.pending.values() if d.status == "pending"],
                "recent": [d.to_json(now) for d in list(self.history)[:10]]}

    # ---- survive restarts ---------------------------------------------------------------
    def to_state(self, clocks: Clocks) -> dict:
        def dec(d: Decision) -> dict:
            return {**d.__dict__, "created_at": clocks.to_wall(d.created_at), "deadline_at": clocks.to_wall(d.deadline_at)}

        return {"enabled": self.enabled, "seq": next(self._seq), "pending": [dec(d) for d in self.pending.values()],
                "history": [dec(d) for d in self.history]}

    def from_state(self, data: dict, clocks: Clocks) -> None:
        def dec(x: dict) -> Decision:
            return Decision(**{**x, "created_at": clocks.to_mono(x["created_at"]), "deadline_at": clocks.to_mono(x["deadline_at"])})

        self.enabled = data.get("enabled", self.enabled)
        self.pending = {d.id: d for d in (dec(x) for x in data.get("pending", []))}
        self.history = deque((dec(x) for x in data.get("history", [])), maxlen=KEEP_HISTORY)
        self._seq = itertools.count(data.get("seq", 1))
