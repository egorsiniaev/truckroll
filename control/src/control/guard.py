"""Abuse limits for the public demo: anyone may drive it, nobody may flood it."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from fastapi import Request

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class Limits:
    writes_per_ip: int = 20  # per window
    writes_total: int = 200  # per window, all visitors together
    reads_per_ip: int = 120  # per window, /api only; static files are not counted
    window_s: float = 60.0
    streams_per_ip: int = 4
    streams_total: int = 200


def client_ip(request: Request) -> str:
    """Caddy overwrites X-Forwarded-For with the peer address, so its last entry is the real client."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


@dataclass
class SlidingWindow:
    limit: int
    window_s: float
    clock: Callable[[], float] = time.monotonic
    _hits: dict[str, deque[float]] = field(default_factory=dict)

    def allow(self, key: str) -> bool:
        now = self.clock()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= self.window_s:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True

    def prune(self) -> None:
        now = self.clock()
        for key in [k for k, h in self._hits.items() if not h or now - h[-1] >= self.window_s]:
            del self._hits[key]


DEFAULT_LIMITS = Limits()


class Guard:
    def __init__(self, limits: Limits = DEFAULT_LIMITS, clock: Callable[[], float] = time.monotonic):
        self.limits = limits
        self._writes_ip = SlidingWindow(limits.writes_per_ip, limits.window_s, clock)
        self._writes_all = SlidingWindow(limits.writes_total, limits.window_s, clock)
        self._reads_ip = SlidingWindow(limits.reads_per_ip, limits.window_s, clock)
        self._streams: dict[str, int] = {}

    def allow_request(self, method: str, ip: str) -> bool:
        self._prune_now_and_then()
        if method in READ_METHODS:
            return self._reads_ip.allow(ip)
        return self._writes_ip.allow(ip) and self._writes_all.allow("*")

    def open_stream(self, ip: str) -> bool:
        if sum(self._streams.values()) >= self.limits.streams_total:
            return False
        if self._streams.get(ip, 0) >= self.limits.streams_per_ip:
            return False
        self._streams[ip] = self._streams.get(ip, 0) + 1
        return True

    def close_stream(self, ip: str) -> None:
        left = self._streams.get(ip, 0) - 1
        if left > 0:
            self._streams[ip] = left
        else:
            self._streams.pop(ip, None)

    def _prune_now_and_then(self) -> None:
        if len(self._reads_ip._hits) + len(self._writes_ip._hits) > 10_000:
            for w in (self._reads_ip, self._writes_ip):
                w.prune()
