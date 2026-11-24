"""OCPP 1.6J handlers for one connected station."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

from ocpp.routing import on
from ocpp.v16 import ChargePoint, call_result
from ocpp.v16.enums import Action, AuthorizationStatus, RegistrationStatus

from csms.auth import TagList
from csms.chaos import Chaos
from csms.fleet import FleetStore, site_of
from csms.telemetry import Telemetry

log = logging.getLogger(__name__)

CALL = 2
POWER_UNITS = {"W": 1.0, "kW": 1000.0}
ENERGY_UNITS = {"Wh": 1.0, "kWh": 1000.0}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_meter_values(meter_value: list[dict]) -> tuple[float | None, float | None]:
    """Return (power W, energy Wh) from the latest sample that carries each."""
    power_w = energy_wh = None
    for sample in meter_value:
        for sv in sample.get("sampled_value", []):
            measurand = sv.get("measurand", "Energy.Active.Import.Register")
            try:
                value = float(sv["value"])
            except (KeyError, TypeError, ValueError):
                continue
            if measurand == "Power.Active.Import":
                power_w = value * POWER_UNITS.get(sv.get("unit", "W"), 1.0)
            elif measurand == "Energy.Active.Import.Register":
                energy_wh = value * ENERGY_UNITS.get(sv.get("unit", "Wh"), 1.0)
    return power_w, energy_wh


class StationConnection(ChargePoint):
    def __init__(
        self,
        station_id: str,
        connection,
        fleet: FleetStore,
        telemetry: Telemetry,
        auth: TagList,
        heartbeat_interval_s: int,
        clock: Callable[[], float] = time.time,
        chaos: Chaos | None = None,
    ) -> None:
        super().__init__(station_id, connection)
        self._chaos = chaos or Chaos()
        self._fleet = fleet
        self._telemetry = telemetry
        self._auth = auth
        self._interval = heartbeat_interval_s
        self._clock = clock
        self.site = site_of(station_id)

    async def route_message(self, raw_msg):
        try:
            msg = json.loads(raw_msg)
            if isinstance(msg, list) and len(msg) >= 3 and msg[0] == CALL:
                self._telemetry.message(str(msg[2]), self.site)
        except (json.JSONDecodeError, TypeError):
            log.warning("station=%s sent unparseable frame", self.id)
        self._fleet.seen(self.id, self._clock())
        await self._chaos.delay_reply()
        return await super().route_message(raw_msg)

    @on(Action.boot_notification)
    async def on_boot(self, charge_point_vendor, charge_point_model, **kwargs):
        firmware = kwargs.get("firmware_version", "")
        self._fleet.boot(self.id, charge_point_vendor, charge_point_model, firmware, self._clock())
        for tx in self._fleet.close_stale(self.id):
            self._telemetry.session_stopped(self.site, "Reboot")
            log.warning("station=%s site=%s closed stale tx=%s after reboot", self.id, self.site, tx)
        log.info("station=%s site=%s boot vendor=%s model=%s fw=%s",
                 self.id, self.site, charge_point_vendor, charge_point_model, firmware)
        return call_result.BootNotification(
            current_time=_now_iso(), interval=self._interval, status=RegistrationStatus.accepted
        )

    @on(Action.heartbeat)
    async def on_heartbeat(self):
        return call_result.Heartbeat(current_time=_now_iso())

    @on(Action.authorize)
    async def on_authorize(self, id_tag):
        await self._chaos.delay_auth()
        return call_result.Authorize(id_tag_info={"status": self._tag_status(id_tag)})

    @on(Action.start_transaction)
    async def on_start_transaction(self, connector_id, id_tag, meter_start, timestamp, **kwargs):
        await self._chaos.delay_auth()
        status = self._tag_status(id_tag)
        if status != AuthorizationStatus.accepted:
            self._telemetry.session_rejected(self.site, status.value)
            log.warning("station=%s site=%s connector=%s session rejected tag_status=%s",
                        self.id, self.site, connector_id, status.value)
            return call_result.StartTransaction(transaction_id=0, id_tag_info={"status": status})
        tx_id = self._fleet.start_transaction(self.id, connector_id, self._clock(), float(meter_start))
        self._telemetry.session_started(self.site)
        log.info("station=%s site=%s connector=%s session started tx=%s", self.id, self.site, connector_id, tx_id)
        return call_result.StartTransaction(transaction_id=tx_id, id_tag_info={"status": status})

    @on(Action.stop_transaction)
    async def on_stop_transaction(self, meter_stop, timestamp, transaction_id, **kwargs):
        reason = kwargs.get("reason") or "Local"
        owner = self._fleet.stop_transaction(transaction_id, self._clock())
        if owner is None:
            log.warning("station=%s site=%s stop for unknown tx=%s", self.id, self.site, transaction_id)
        self._telemetry.session_stopped(self.site, reason)
        log.info("station=%s site=%s session stopped tx=%s reason=%s meter_stop=%s",
                 self.id, self.site, transaction_id, reason, meter_stop)
        return call_result.StopTransaction()

    @on(Action.status_notification)
    async def on_status_notification(self, connector_id, error_code, status, **kwargs):
        self._fleet.set_status(self.id, connector_id, status, error_code, self._clock())
        if status == "Available":
            for tx in self._fleet.close_stale(self.id, connector_id):
                self._telemetry.session_stopped(self.site, "StaleAvailable")
                log.warning("station=%s site=%s connector=%s closed stale tx=%s: connector reports Available",
                            self.id, self.site, connector_id, tx)
        level = logging.WARNING if status == "Faulted" else logging.INFO
        log.log(level, "station=%s site=%s connector=%s status=%s error_code=%s",
                self.id, self.site, connector_id, status, error_code)
        return call_result.StatusNotification()

    @on(Action.meter_values)
    async def on_meter_values(self, connector_id, meter_value, **kwargs):
        tx = kwargs.get("transaction_id")
        if tx and connector_id and self._fleet.adopt_transaction(self.id, connector_id, int(tx), self._clock()):
            log.warning("station=%s site=%s connector=%s adopted tx=%s started before a CSMS restart",
                        self.id, self.site, connector_id, tx)
        power_w, energy_wh = parse_meter_values(meter_value)
        factor = self._chaos.meter_factor.get(self.id)
        if factor and energy_wh is not None:
            # A drifting meter: the register runs fast, relative to where the session started.
            start = (self._fleet.get(self.id).connectors.get(connector_id) or None)
            base = start.meter_start_wh if start and start.meter_start_wh is not None else 0.0
            energy_wh = base + (energy_wh - base) * factor
        self._fleet.record_meter(self.id, connector_id, power_w, energy_wh, self._clock())
        return call_result.MeterValues()

    def _tag_status(self, id_tag: str) -> AuthorizationStatus:
        if self._chaos.free_vend or self._auth.is_valid(id_tag):
            return AuthorizationStatus.accepted
        return AuthorizationStatus.invalid
