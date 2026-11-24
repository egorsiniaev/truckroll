"""Entrypoint: read env, wire OpenTelemetry, run the OCPP server until SIGTERM."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from opentelemetry import metrics
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

from csms.auth import load_tag_list
from csms.fleet import FleetStore
from csms.server import serve
from csms.telemetry import Telemetry

log = logging.getLogger("csms")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from None


def _setup_otel() -> MeterProvider:
    # OTEL_EXPORTER_OTLP_ENDPOINT selects the collector; without it, telemetry is stdout logs only.
    resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", "csms")})
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        readers = [PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=10_000)]
        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
        set_logger_provider(logger_provider)
        handlers.append(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))
    else:
        readers = []
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
    )
    # The ocpp library logs every frame at INFO; keep it for debugging only.
    logging.getLogger("ocpp").setLevel(logging.WARNING)
    # websockets logs every health check, and its log extras are not valid OTel attributes.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    provider = MeterProvider(resource=resource, metric_readers=readers)
    metrics.set_meter_provider(provider)
    return provider


async def _run() -> None:
    provider = _setup_otel()
    fleet = FleetStore()
    telemetry = Telemetry(provider.get_meter("csms"), fleet)
    auth = load_tag_list(os.environ.get("CSMS_ID_TAGS_FILE"), os.environ.get("CSMS_ACCEPT_ANY_TAG") == "true")
    host = os.environ.get("CSMS_HOST", "0.0.0.0")
    port = _env_int("CSMS_PORT", 9000)
    server = await serve(host, port, fleet, telemetry, auth, _env_int("CSMS_HEARTBEAT_INTERVAL_S", 30))
    log.info("listening on ws://%s:%d/<station_id>", host, port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("shutting down")
    server.close()
    await server.wait_closed()
    provider.shutdown()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
