"""Turn monotonic timestamps into wall-clock ones and back, so timers keep running across a restart."""

from __future__ import annotations

import time


class Clocks:
    """Snapshot of both clocks at one moment; converts in either direction."""

    def __init__(self, mono: float | None = None, wall: float | None = None) -> None:
        self.mono = time.monotonic() if mono is None else mono
        self.wall = time.time() if wall is None else wall

    def to_wall(self, t: float | None) -> float | None:
        return None if t is None else self.wall - (self.mono - t)

    def to_mono(self, w: float | None) -> float | None:
        return None if w is None else self.mono - (self.wall - w)
