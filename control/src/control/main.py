"""Entrypoint: read env, build clients, serve API + web page."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx
import uvicorn

from control.app import Clients, Hub, create_app
from control.clients import CsmsClient, SimulatorClient, ToxiproxyClient, WeatherClient
from control.prices import PriceClient


def load_sites(path: str) -> list[dict]:
    try:
        sites = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read sites file {path}: {exc}") from exc
    required = {"code", "lat", "lon", "cap_w", "proxy", "stations", "connectors"}
    for s in sites:
        missing = required - s.keys()
        if missing:
            raise SystemExit(f"site {s.get('code', '?')} missing {sorted(missing)}")
    return sites


def build_app():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per poll otherwise
    http = httpx.AsyncClient()
    clients = Clients(
        csms=CsmsClient(http, os.environ.get("CSMS_ADMIN_URL", "http://csms:9001")),
        toxiproxy=ToxiproxyClient(http, os.environ.get("TOXIPROXY_URL", "http://toxiproxy:8474")),
        simulator=SimulatorClient(os.environ.get("SIMULATOR_UI_URL", "ws://simulator:8080")),
        weather=None if os.environ.get("WEATHER_ENABLED", "true") == "false" else WeatherClient(http),
        prices=PriceClient(http, token) if (token := os.environ.get("ENTSOE_API_TOKEN")) else None,
    )
    if clients.prices is None:
        logging.getLogger(__name__).warning("ENTSOE_API_TOKEN not set: price data disabled")
    ledger = os.environ.get("LEDGER_FILE", "/data/ledger.jsonl")
    flag = lambda name, default: os.environ.get(name, default).lower() == "true"  # noqa: E731
    hub = Hub(sites=load_sites(os.environ.get("SITES_FILE", "/config/sites.json")), clients=clients,
              ledger_path=Path(ledger) if ledger else None,
              world_default=flag("WORLD_DEFAULT", "true"), autopilot_default=flag("AUTOPILOT_DEFAULT", "true"),
              grafana_url=os.environ.get("GRAFANA_URL", "http://localhost:3000/d/truckroll-fleet"),
              dynamic_pricing_default=flag("DYNAMIC_PRICING_DEFAULT", "true"),
              batteries_default=flag("BATTERIES_DEFAULT", "true"))
    web = os.environ.get("WEB_DIR", "/web")
    return create_app(hub, Path(web))


def main() -> None:
    # Page streams (SSE) never finish on their own; cap the wait so shutdown (and the state save) always runs
    # before Docker's 10 s kill.
    uvicorn.run(build_app(), host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")),
                log_level="warning", timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
