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
    )
    ledger = os.environ.get("LEDGER_FILE", "/data/ledger.jsonl")
    hub = Hub(sites=load_sites(os.environ.get("SITES_FILE", "/config/sites.json")), clients=clients,
              ledger_path=Path(ledger) if ledger else None)
    web = os.environ.get("WEB_DIR", "/web")
    return create_app(hub, Path(web))


def main() -> None:
    uvicorn.run(build_app(), host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")),
                log_level="warning")


if __name__ == "__main__":
    main()
