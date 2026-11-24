"""Starts a random scenario every 5-10 minutes, so the map always has something going on."""

from __future__ import annotations

import random
import time
from collections.abc import Callable

MIN_GAP_S, MAX_GAP_S = 300, 600


class Autopilot:
    def __init__(self, runner, enabled: bool = True, clock: Callable[[], float] = time.monotonic,
                 rng: random.Random | None = None) -> None:
        self.runner = runner
        self.enabled = enabled
        self.clock = clock
        self.rng = rng or random.Random()
        self.next_at = self.clock() + self._gap()
        self.last: str | None = None

    def _gap(self) -> float:
        return self.rng.uniform(MIN_GAP_S, MAX_GAP_S)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        if enabled:
            self.next_at = self.clock() + self._gap()

    def status(self) -> dict:
        busy = self.runner.current is not None
        return {"enabled": self.enabled, "last": self.last,
                "next_in_s": None if not self.enabled or busy else max(0, round(self.next_at - self.clock()))}

    def tick(self, stations: list[dict]) -> str | None:
        """Starts a scenario when due. Returns its id, or None."""
        if not self.enabled:
            return None
        if self.runner.current is not None:
            # Count the gap from the end of the running scenario.
            self.next_at = max(self.next_at, self.clock() + self._gap())
            return None
        if self.clock() < self.next_at or not stations:
            return None
        choices = [s["id"] for s in self.runner.catalog() if s["id"] != self.last]
        pick = self.rng.choice(choices)
        self.runner.start(pick, stations)
        self.last = pick
        self.next_at = self.clock() + self._gap()
        return pick
