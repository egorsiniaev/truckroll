"""Fault injection for incident scenarios: slow auth, drifting meters, ignored profiles, a slow backend.

Only the control service's scenarios use this, through the internal admin API. Everything defaults to off.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

EWMA = 0.3


@dataclass
class Stats:
    """What the backend can observe about itself: how slow replies and the auth backend are right now."""

    reply_latency_s: float = 0.0
    auth_latency_s: float = 0.0
    replies: int = 0

    def reply(self, seconds: float) -> None:
        self.reply_latency_s = seconds if not self.replies else EWMA * seconds + (1 - EWMA) * self.reply_latency_s
        self.replies += 1

    def auth(self, seconds: float) -> None:
        self.auth_latency_s = EWMA * seconds + (1 - EWMA) * self.auth_latency_s

    def to_json(self) -> dict:
        return {"reply_latency_s": round(self.reply_latency_s, 2), "auth_latency_s": round(self.auth_latency_s, 2)}


@dataclass
class Chaos:
    auth_delay_s: float = 0.0  # delay on Authorize and StartTransaction replies
    response_delay_s: float = 0.0  # delay on every reply: an overloaded backend
    meter_factor: dict[str, float] = field(default_factory=dict)  # station -> multiplier on energy readings
    drop_profiles: set[str] = field(default_factory=set)  # stations whose limits are "accepted" but never sent
    free_vend: bool = False  # accept every RFID tag without asking the auth backend (local fallback)
    stats: Stats = field(default_factory=Stats)

    async def _auth_backend(self) -> None:
        """Stands in for the call to the RFID auth backend; slow when auth_delay_s is set."""
        t0 = time.monotonic()
        if self.auth_delay_s > 0:
            await asyncio.sleep(self.auth_delay_s)
        self.stats.auth(time.monotonic() - t0)

    async def delay_auth(self) -> None:
        if self.free_vend:
            # Don't wait for the backend, but keep probing it so we can tell when it has recovered.
            asyncio.get_running_loop().create_task(self._auth_backend())
            return
        await self._auth_backend()

    async def delay_reply(self) -> None:
        t0 = time.monotonic()
        if self.response_delay_s > 0:
            await asyncio.sleep(self.response_delay_s)
        self.stats.reply(time.monotonic() - t0)

    def to_json(self) -> dict:
        return {"auth_delay_s": self.auth_delay_s, "response_delay_s": self.response_delay_s,
                "meter_factor": dict(self.meter_factor), "drop_profiles": sorted(self.drop_profiles),
                "free_vend": self.free_vend, "stats": self.stats.to_json()}
