"""Generate simulator config, station templates, id tags and Toxiproxy proxies from one site table.

Run: python3 fleet/generate.py  (stdlib only; output is committed)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

OUT = Path(__file__).parent
CSMS_UPSTREAM = "csms:9000"
FIRST_PROXY_PORT = 8001
ID_TAGS = [f"TR-{n:04d}" for n in range(1, 101)]


@dataclass(frozen=True)
class Site:
    code: str  # becomes the station id prefix, so the CSMS can derive the site
    kind: str
    stations: int
    connectors: int
    power_w: int
    current: str  # AC or DC
    vendor: str
    model: str
    session_s: tuple[int, int]
    gap_s: tuple[int, int]


SITES = (
    Site("AMS-DEPOT", "logistics depot", 20, 1, 22_080, "AC", "Truckroll Sim", "AC22-DEPOT", (900, 2400), (120, 900)),
    Site("UTR-RETAIL", "retail parking", 12, 2, 22_080, "AC", "Truckroll Sim", "AC22-DUO", (600, 1800), (60, 600)),
    Site("A2-HUB", "highway fast-charging hub", 8, 2, 150_000, "DC", "Truckroll Sim", "DC150", (600, 1500), (60, 400)),
    Site("EHV-OFFICE", "office campus", 10, 1, 11_040, "AC", "Truckroll Sim", "AC11-OFFICE", (1800, 3600), (300, 1800)),
)


def proxy_port(index: int) -> int:
    return FIRST_PROXY_PORT + index


def template(site: Site, index: int) -> dict:
    meter = [
        {"unit": "Wh", "context": "Sample.Periodic", "measurand": "Energy.Active.Import.Register"},
        {"unit": "W", "context": "Sample.Periodic", "measurand": "Power.Active.Import"},
    ]
    connectors = {"0": {}} | {
        str(c): {"bootStatus": "Available", "MeterValues": meter} for c in range(1, site.connectors + 1)
    }
    return {
        "$schemaVersion": 2,
        "supervisionUrls": f"ws://toxiproxy:{proxy_port(index)}",
        "idTagsFile": "idtags.json",
        "baseName": site.code,
        "chargePointModel": site.model,
        "chargePointVendor": site.vendor,
        "firmwareVersion": "1.0.0",
        "power": site.power_w,
        "powerUnit": "W",
        "powerSharedByConnectors": site.connectors > 1,
        "currentOutType": site.current,
        "numberOfConnectors": site.connectors,
        "randomConnectors": False,
        "Configuration": {
            "configurationKey": [
                {"key": "MeterValuesSampledData", "readonly": False,
                 "value": "Energy.Active.Import.Register,Power.Active.Import"},
                {"key": "MeterValueSampleInterval", "readonly": False, "value": "30"},
                {"key": "SupportedFeatureProfiles", "readonly": True,
                 "value": "Core,FirmwareManagement,SmartCharging,RemoteTrigger"},
                {"key": "AuthorizeRemoteTxRequests", "readonly": False, "value": "false"},
                {"key": "WebSocketPingInterval", "readonly": False, "value": "60"},
            ]
        },
        "AutomaticTransactionGenerator": {
            "enable": True,
            "minDuration": site.session_s[0],
            "maxDuration": site.session_s[1],
            "minDelayBetweenTwoTransactions": site.gap_s[0],
            "maxDelayBetweenTwoTransactions": site.gap_s[1],
            "probabilityOfStart": 0.8,
            "stopAfterHours": 8760,
            "requireAuthorize": True,
        },
        "Connectors": connectors,
    }


def template_file(site: Site) -> str:
    return f"truckroll-{site.code.lower()}.station-template.json"


def simulator_config() -> dict:
    return {
        "supervisionUrls": [f"ws://toxiproxy:{FIRST_PROXY_PORT}"],
        "supervisionUrlDistribution": "round-robin",
        "log": {"file": "/dev/stdout", "errorFile": "/dev/stderr", "console": False, "rotate": False},
        "worker": {"processType": "workerSet", "elementsPerWorker": 10, "poolMinSize": 2, "poolMaxSize": 8},
        "uiServer": {"enabled": False},
        "stationTemplateUrls": [{"file": template_file(s), "numberOfStations": s.stations} for s in SITES],
    }


def toxiproxy_config() -> list[dict]:
    return [
        {"name": s.code.lower(), "listen": f"0.0.0.0:{proxy_port(i)}", "upstream": CSMS_UPSTREAM, "enabled": True}
        for i, s in enumerate(SITES)
    ]


def write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def main() -> None:
    write(OUT / "config.json", simulator_config())
    write(OUT / "idtags.json", ID_TAGS)
    write(OUT / "toxiproxy.json", toxiproxy_config())
    for i, site in enumerate(SITES):
        write(OUT / "station-templates" / template_file(site), template(site, i))
    total = sum(s.stations for s in SITES)
    print(f"{len(SITES)} sites, {total} stations, {sum(s.stations * s.connectors for s in SITES)} connectors")


if __name__ == "__main__":
    main()
