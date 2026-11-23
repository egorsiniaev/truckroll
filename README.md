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
| `.github/workflows/` | SBOM (Syft) and vulnerability scan (Grype) |

## Status

Skeleton only. Nothing runs yet.

## Results

Eval numbers, hours spent, and what the agent got wrong will go here as they come in. No numbers yet.

## Safety

Low voltage only. Nothing connects to mains or a real EV.
