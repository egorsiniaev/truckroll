"""OpenTelemetry instruments. Gauges read the fleet snapshot at export time."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable

from ocpp.v16.enums import ChargePointErrorCode, ChargePointStatus
from opentelemetry.metrics import CallbackOptions, Meter, Observation

from csms.fleet import FleetStore, Station

# Every value is reported as 0/1 so a changed status never leaves a stale series at 1.
STATUSES = tuple(s.value for s in ChargePointStatus)
ERROR_CODES = tuple(e.value for e in ChargePointErrorCode if e.value != "NoError")


def _station_attrs(s: Station) -> dict[str, str]:
    return {"station": s.station_id, "site": s.site}


class Telemetry:
    def __init__(self, meter: Meter, fleet: FleetStore, clock: Callable[[], float] = time.time) -> None:
        self._fleet = fleet
        self._clock = clock
        self._messages = meter.create_counter("ocpp.messages", description="OCPP calls received from stations")
        self._started = meter.create_counter("csms.sessions.started", description="Accepted StartTransaction")
        self._rejected = meter.create_counter("csms.sessions.rejected", description="Rejected StartTransaction")
        self._stopped = meter.create_counter("csms.sessions.stopped", description="StopTransaction received")
        meter.create_observable_gauge(
            "csms.station.connected", [self._observe_connected], description="1 if the station has an open socket"
        )
        meter.create_observable_gauge(
            "csms.station.heartbeat.age", [self._observe_heartbeat_age], unit="s",
            description="Seconds since the station last sent anything",
        )
        meter.create_observable_gauge(
            "csms.connector.status", [self._observe_status], description="1 for the connector's current status, else 0"
        )
        meter.create_observable_gauge(
            "csms.connector.error", [self._observe_error], description="1 for the connector's current error code, else 0"
        )
        meter.create_observable_gauge(
            "csms.connector.power", [self._observe_power], unit="W", description="Last reported active power"
        )
        meter.create_observable_gauge(
            "csms.connector.energy.wh", [self._observe_energy], description="Last reported energy register, Wh"
        )

    def message(self, action: str, site: str) -> None:
        self._messages.add(1, {"action": action, "site": site})

    def session_started(self, site: str) -> None:
        self._started.add(1, {"site": site})

    def session_rejected(self, site: str, reason: str) -> None:
        self._rejected.add(1, {"site": site, "reason": reason})

    def session_stopped(self, site: str, reason: str) -> None:
        self._stopped.add(1, {"site": site, "reason": reason})

    def _observe_connected(self, _: CallbackOptions) -> Iterable[Observation]:
        return [Observation(int(s.connected), _station_attrs(s)) for s in self._fleet.snapshot()]

    def _observe_heartbeat_age(self, _: CallbackOptions) -> Iterable[Observation]:
        now = self._clock()
        return [Observation(max(0.0, now - s.last_seen), _station_attrs(s)) for s in self._fleet.snapshot()]

    def _connectors(self) -> Iterable[tuple[Station, int, object]]:
        # Connector 0 means the whole station in OCPP 1.6; availability counts real connectors only.
        for s in self._fleet.snapshot():
            for cid, c in s.connectors.items():
                if cid > 0:
                    yield s, cid, c

    def _observe_status(self, _: CallbackOptions) -> Iterable[Observation]:
        return [
            Observation(int(c.status == status), {**_station_attrs(s), "connector": str(cid), "status": status})
            for s, cid, c in self._connectors()
            for status in STATUSES
        ]

    def _observe_error(self, _: CallbackOptions) -> Iterable[Observation]:
        return [
            Observation(int(c.error_code == code), {**_station_attrs(s), "connector": str(cid), "error_code": code})
            for s, cid, c in self._connectors()
            for code in ERROR_CODES
        ]

    def _observe_power(self, _: CallbackOptions) -> Iterable[Observation]:
        return [Observation(c.power_w, {**_station_attrs(s), "connector": str(cid)}) for s, cid, c in self._connectors()]

    def _observe_energy(self, _: CallbackOptions) -> Iterable[Observation]:
        return [Observation(c.energy_wh, {**_station_attrs(s), "connector": str(cid)}) for s, cid, c in self._connectors()]
