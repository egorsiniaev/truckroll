"""In-memory fleet state. Every update swaps in new frozen records, so readers on other threads see a consistent snapshot."""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType


def site_of(station_id: str) -> str:
    head, sep, _ = station_id.rpartition("-")
    return head if sep and head else station_id


@dataclass(frozen=True)
class Connector:
    status: str = "Unknown"
    error_code: str = "NoError"
    power_w: float = 0.0
    energy_wh: float = 0.0
    transaction_id: int | None = None
    meter_start_wh: float | None = None  # register reading at session start, for energy per session


@dataclass(frozen=True)
class Station:
    station_id: str
    site: str
    connected: bool = False
    last_seen: float = 0.0
    vendor: str = ""
    model: str = ""
    firmware: str = ""
    limit_w: float | None = None
    connectors: Mapping[int, Connector] = field(default_factory=lambda: MappingProxyType({}))


def _with_connector(station: Station, connector_id: int, **changes) -> Station:
    current = station.connectors.get(connector_id, Connector())
    connectors = {**station.connectors, connector_id: replace(current, **changes)}
    return replace(station, connectors=MappingProxyType(connectors))


class FleetStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stations: Mapping[str, Station] = MappingProxyType({})
        self._transactions: Mapping[int, tuple[str, int]] = MappingProxyType({})
        self._tokens: dict[str, int] = {}
        self._token_seq = itertools.count(1)
        # Start transaction ids from the clock, so ids from before a restart never collide with new ones.
        self._tx_seq = itertools.count(int(time.time()))

    def get(self, station_id: str) -> Station | None:
        return self._stations.get(station_id)

    def snapshot(self) -> tuple[Station, ...]:
        return tuple(self._stations.values())

    def _put(self, station: Station) -> None:
        self._stations = MappingProxyType({**self._stations, station.station_id: station})

    def _existing(self, station_id: str) -> Station:
        return self._stations.get(station_id) or Station(station_id=station_id, site=site_of(station_id))

    def connect(self, station_id: str, now: float) -> int:
        with self._lock:
            token = next(self._token_seq)
            self._tokens[station_id] = token
            self._put(replace(self._existing(station_id), connected=True, last_seen=now))
            return token

    def disconnect(self, station_id: str, token: int) -> None:
        with self._lock:
            # A newer connection for the same station owns its state now.
            if self._tokens.get(station_id) != token:
                return
            self._put(replace(self._existing(station_id), connected=False))

    def seen(self, station_id: str, now: float) -> None:
        with self._lock:
            self._put(replace(self._existing(station_id), last_seen=now))

    def boot(self, station_id: str, vendor: str, model: str, firmware: str, now: float) -> None:
        with self._lock:
            station = replace(self._existing(station_id), vendor=vendor, model=model, firmware=firmware, last_seen=now)
            self._put(station)

    def set_status(self, station_id: str, connector_id: int, status: str, error_code: str, now: float) -> None:
        with self._lock:
            station = _with_connector(self._existing(station_id), connector_id, status=status, error_code=error_code)
            self._put(replace(station, last_seen=now))

    def start_transaction(self, station_id: str, connector_id: int, now: float, meter_start_wh: float | None = None) -> int:
        with self._lock:
            tx_id = next(self._tx_seq)
            self._transactions = MappingProxyType({**self._transactions, tx_id: (station_id, connector_id)})
            station = _with_connector(self._existing(station_id), connector_id, transaction_id=tx_id,
                                      meter_start_wh=meter_start_wh)
            self._put(replace(station, last_seen=now))
            return tx_id

    def stop_transaction(self, tx_id: int, now: float) -> str | None:
        with self._lock:
            owner = self._transactions.get(tx_id)
            if owner is None:
                return None
            station_id, connector_id = owner
            self._transactions = MappingProxyType({k: v for k, v in self._transactions.items() if k != tx_id})
            station = _with_connector(self._existing(station_id), connector_id, transaction_id=None, power_w=0.0,
                                      meter_start_wh=None)
            self._put(replace(station, last_seen=now))
            return station_id

    def knows_transaction(self, tx_id: int) -> bool:
        return tx_id in self._transactions

    def adopt_transaction(self, station_id: str, connector_id: int, tx_id: int, now: float) -> bool:
        """Take over a transaction started before a CSMS restart (seen in MeterValues). Energy per session is
        unknown for it. Returns False if the id is already known."""
        with self._lock:
            if tx_id in self._transactions:
                return False
            self._transactions = MappingProxyType({**self._transactions, tx_id: (station_id, connector_id)})
            station = _with_connector(self._existing(station_id), connector_id, transaction_id=tx_id, meter_start_wh=None)
            self._put(replace(station, last_seen=now))
            return True

    def close_stale(self, station_id: str, connector_id: int | None = None) -> list[int]:
        """Close transactions the station can no longer have: all of them after a reboot (connector None), or one
        connector's when it reports Available. Returns the closed transaction ids."""
        with self._lock:
            stale = [tx for tx, (sid, cid) in self._transactions.items()
                     if sid == station_id and (connector_id is None or cid == connector_id)]
            if not stale:
                return []
            self._transactions = MappingProxyType({k: v for k, v in self._transactions.items() if k not in stale})
            station = self._existing(station_id)
            for cid, conn in station.connectors.items():
                if conn.transaction_id in stale:
                    station = _with_connector(station, cid, transaction_id=None, power_w=0.0, meter_start_wh=None)
            self._put(station)
            return stale

    def set_limit(self, station_id: str, watts: float | None) -> None:
        with self._lock:
            self._put(replace(self._existing(station_id), limit_w=watts))

    def record_meter(
        self, station_id: str, connector_id: int, power_w: float | None, energy_wh: float | None, now: float
    ) -> None:
        with self._lock:
            changes = {k: v for k, v in (("power_w", power_w), ("energy_wh", energy_wh)) if v is not None}
            station = _with_connector(self._existing(station_id), connector_id, **changes)
            self._put(replace(station, last_seen=now))
