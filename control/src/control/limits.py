"""One place that decides each station's power limit. Several sources may want a limit; the lowest wins."""

from __future__ import annotations

import logging

from control.clients import UpstreamError

log = logging.getLogger(__name__)

SOURCE_LABELS = {
    "operator": "operator",
    "site-controller": "grid cap",
    "cold": "cold derate",
    "price": "price",
    "grid-operator": "grid operator request",
}


class LimitArbiter:
    def __init__(self, csms) -> None:
        self._csms = csms
        self._wanted: dict[str, dict[str, float]] = {}  # source -> station -> watts
        self._applied: dict[str, float | None] = {}
        self._pending: set[str] = set()

    def effective(self, station: str) -> float | None:
        values = [m[station] for m in self._wanted.values() if station in m]
        return min(values) if values else None

    def sources_for(self, station: str) -> dict[str, float]:
        return {src: m[station] for src, m in self._wanted.items() if station in m}

    def stations_for(self, source: str) -> dict[str, float]:
        return dict(self._wanted.get(source, {}))

    async def set_many(self, source: str, limits: dict[str, float | None]) -> None:
        wanted = dict(self._wanted.get(source, {}))
        for station, watts in limits.items():
            if watts is None:
                wanted.pop(station, None)
            else:
                wanted[station] = round(watts)
        self._wanted = {**self._wanted, source: wanted}
        for station in limits:
            await self._apply(station)

    async def set(self, source: str, station: str, watts: float | None) -> None:
        await self.set_many(source, {station: watts})

    async def clear_source(self, source: str, stations: list[str] | None = None) -> list[str]:
        current = self._wanted.get(source, {})
        targets = [s for s in current if stations is None or s in stations]
        await self.set_many(source, {s: None for s in targets})
        return targets

    async def _apply(self, station: str) -> None:
        eff = self.effective(station)
        if station in self._applied and self._applied[station] == eff and station not in self._pending:
            return
        try:
            await self._csms.set_limit(station, eff)
            self._applied[station] = eff
            self._pending.discard(station)
        except UpstreamError as exc:
            # Usually the station is offline; retry when it is back.
            self._pending.add(station)
            log.info("limit for %s deferred: %s", station, exc)

    async def retry_pending(self, online: set[str]) -> None:
        for station in sorted(self._pending & online):
            await self._apply(station)

    async def reconcile(self, stations: list[dict]) -> list[str]:
        """Clear limits nobody asked for (left over from a restart). Returns the stations cleared."""
        cleared = []
        for st in stations:
            if st.get("limit_w") is not None and self.effective(st["id"]) is None and st.get("connected"):
                try:
                    await self._csms.set_limit(st["id"], None)
                    self._applied[st["id"]] = None
                    cleared.append(st["id"])
                except UpstreamError as exc:
                    log.info("reconcile %s failed: %s", st["id"], exc)
        return cleared
