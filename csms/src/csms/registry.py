"""Live station connections, so the admin API can send commands to a station."""

from __future__ import annotations

import threading
from typing import Protocol


class Commandable(Protocol):
    id: str

    async def call(self, payload): ...


class Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._live: dict[str, tuple[int, Commandable]] = {}

    def add(self, station_id: str, token: int, connection: Commandable) -> None:
        with self._lock:
            self._live[station_id] = (token, connection)

    def remove(self, station_id: str, token: int) -> None:
        with self._lock:
            # Only the connection that registered may remove itself; a newer one may already own the slot.
            if self._live.get(station_id, (None,))[0] == token:
                del self._live[station_id]

    def get(self, station_id: str) -> Commandable | None:
        entry = self._live.get(station_id)
        return entry[1] if entry else None
