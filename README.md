# Truckroll: charger fleet ops copilot

A mini charging network on a desk: one real low-voltage charger (ESP32, OCPP 1.6J) and 50 simulated ones,
with SLOs, chaos testing, an AI triage agent behind a human approval gate, and CRA evidence generated in CI.

Goals: [GOALS.md](GOALS.md). Plan: [PLAN.md](PLAN.md). Original spec: [SPEC.md](SPEC.md). Hours: [LOG.md](LOG.md).

## Layout

| Dir | What |
|---|---|
| `firmware/` | ESP32 charger, PlatformIO + MicroOcpp |
| `csms/` | Python CSMS on mobilityhouse/ocpp |
| `fleet/` | SAP charging-stations simulator config |
| `observability/` | OTel, Prometheus, Loki, Grafana (docker compose) |
| `agent/` | Triage agent, approval-gated commands |
| `control/` | Live state, chaos controls, real-world mode, scenarios, incident ledger |
| `web/` | Live isometric page (plain SVG + JS, no build step) |
| `.github/workflows/` | SBOM (Syft) and vulnerability scan (Grype) |

## Status

Runs locally: CSMS, 50 simulated stations behind Toxiproxy, OTel collector, Prometheus, Loki, Grafana, and a
live web page with a control panel, real-world mode (local time, weather, prices), demand and money, batteries,
13 incident scenarios, an incident engine with human-in-the-loop decisions, service levels, and a Gemini triage
agent scored against the incident ledger.
No public server yet; the desk charger comes when the hardware is wired.

## Run locally

Needs Docker with Compose (Colima works).

```bash
docker compose up -d --build   # first build of the simulator takes a few minutes
open http://localhost:8080     # live page: map, details, control panel, scenarios
open http://localhost:3000     # Grafana -> Truckroll fleet
docker compose down            # stop; add -v to drop stored metrics and logs
```

## Results

Eval numbers, hours spent, and what the agent got wrong will go here as they come in. No numbers yet.

## Safety

Low voltage only. Nothing connects to mains or a real EV.
