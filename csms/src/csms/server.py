"""WebSocket server: one StationConnection per OCPP 1.6J socket at ws://host:port/<station_id>."""

from __future__ import annotations

import asyncio
import logging
import time
from http import HTTPStatus

from ocpp.v16 import call
from ocpp.v16.enums import MessageTrigger

from websockets.asyncio.server import Server, ServerConnection
from websockets.asyncio.server import serve as ws_serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from csms.auth import TagList
from csms.charge_point import StationConnection
from csms.fleet import FleetStore
from csms.registry import Registry
from csms.telemetry import Telemetry

log = logging.getLogger(__name__)

SUBPROTOCOL = "ocpp1.6"
RESYNC_DELAY_S = 2.0


async def resync_if_unknown(station: StationConnection, fleet: FleetStore, delay_s: float = RESYNC_DELAY_S) -> None:
    """After a CSMS restart, stations reconnect without booting; ask them to boot so state is rebuilt."""
    await asyncio.sleep(delay_s)
    known = fleet.get(station.id)
    if known is not None and known.model:
        return
    try:
        res = await station.call(call.TriggerMessage(requested_message=MessageTrigger.boot_notification))
        log.info("station=%s resync requested status=%s", station.id, res.status)
    except Exception as exc:  # noqa: BLE001 - a failed resync must not drop the connection
        log.warning("station=%s resync failed: %s", station.id, exc)


def _station_id(path: str) -> str:
    return path.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]


def _health(connection: ServerConnection, request: Request) -> Response | None:
    if request.path == "/healthz":
        return connection.respond(HTTPStatus.OK, "ok\n")
    return None


async def serve(
    host: str,
    port: int,
    fleet: FleetStore,
    telemetry: Telemetry,
    auth: TagList,
    heartbeat_interval_s: int,
    resync_delay_s: float = RESYNC_DELAY_S,
    registry: Registry | None = None,
) -> Server:
    registry = registry if registry is not None else Registry()

    async def handle(connection: ServerConnection) -> None:
        station_id = _station_id(connection.request.path)
        if connection.subprotocol != SUBPROTOCOL or not station_id:
            log.warning("refused connection path=%s subprotocol=%s", connection.request.path, connection.subprotocol)
            await connection.close(code=1002, reason="ocpp1.6 subprotocol and station id required")
            return
        token = fleet.connect(station_id, time.time())
        log.info("station=%s connected remote=%s", station_id, connection.remote_address)
        station = StationConnection(station_id, connection, fleet, telemetry, auth, heartbeat_interval_s)
        registry.add(station_id, token, station)
        resync = asyncio.create_task(resync_if_unknown(station, fleet, resync_delay_s))
        try:
            await station.start()
        except ConnectionClosed as exc:
            log.info("station=%s disconnected code=%s", station_id, exc.rcvd.code if exc.rcvd else None)
        finally:
            resync.cancel()
            registry.remove(station_id, token)
            fleet.disconnect(station_id, token)

    return await ws_serve(handle, host, port, subprotocols=[SUBPROTOCOL], process_request=_health)
