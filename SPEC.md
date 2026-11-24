# Charger fleet ops copilot

Public GitHub repo + 2-minute video + one write-up. 2-3 weekends with Claude Code.

## Pitch (one line)

"A mini charging network on my desk: one real 3D-printed charger and 50 simulated ones, with SLOs, chaos
testing, an AI agent that triages incidents, and CRA evidence generated in CI."

## Architecture

```
[ESP32 mini charger] --OCPP 1.6J/WebSocket--+
[SAP simulator: 50 stations] --toxiproxy-----+--> [CSMS (Python, ocpp lib)] --OTel--> Prometheus / Loki / Grafana
                                                          ^                                   |
                                                          |  OCPP commands (approval gated)   v alerts
                                                          +------------- [Triage agent (Gemini API)]
                                                                           drafts: incident summary, post-mortem,
                                                                           CRA/NIS2 24h early warning
```

## Hardware: the "real" charger (low voltage only, about EUR 50)

Never touches mains or a real car. It behaves like a charger on the protocol, and a 12 V lamp stands in for the car.

| Part | Role | Approx. cost |
|---|---|---|
| ESP32-S3 DevKit | Runs MicroOcpp firmware, Wi-Fi to the CSMS | EUR 10 |
| RC522 RFID reader + 2 cards | Badge in: `Authorize` then `StartTransaction` | EUR 5 |
| Reed switch or toggle | "Cable plugged in": status `Preparing` | EUR 2 |
| Logic-level MOSFET + 12 V LED strip or lamp | The "car" load; PWM dims it when the CSMS sets a charging limit | EUR 8 |
| INA226 current/voltage sensor | Real energy readings for `MeterValues` | EUR 5 |
| WS2812 LED ring | Status colours: Available / Charging / Faulted | EUR 5 |
| 0.96" OLED | kWh, session time, connection state | EUR 4 |
| Push button | Fault injection: sends `StatusNotification: Faulted` | EUR 1 |
| 12 V supply + buck to 5 V | Power | EUR 10 |
| 3D-printed mini wallbox enclosure | Your printer; makes the demo photo | filament |

The demo moment: in Grafana you set a charging profile to 50%, and the lamp on the desk dims to half.

Stretch: emulate the IEC 61851 Control Pilot (states A/B/C via resistors on an ADC pin) so the "plug in" is the real
signalling sequence, not a switch.

Firmware: [MicroOcpp](https://github.com/matth-x/MicroOcpp) (OCPP 1.6 full, 2.0.1 alpha behind `MO_ENABLE_V201=1`; used by
OpenEVSE). Use 1.6J for the device; 2.0.1 is a stretch goal. Build with PlatformIO, signed OTA updates.

## Software: open-source building blocks

| Piece | Choice | Notes |
|---|---|---|
| Fleet simulator | [SAP e-mobility-charging-stations-simulator](https://github.com/SAP/e-mobility-charging-stations-simulator) | Node.js; spawns many stations from templates; OCPP 1.6 / 2.0 / 2.0.1; strict schema validation mode |
| CSMS (backend) | Own thin service on [mobilityhouse/ocpp](https://github.com/mobilityhouse/ocpp) (Python) | ~300 lines; you own the telemetry. Alternatives if you don't want to write it: SteVe (Java, 1.6) or [CitrineOS](https://lfenergy.org/projects/citrineos/) (LF Energy, 2.0.1 + OCPI) |
| Network chaos | Toxiproxy between simulator and CSMS | Latency, dropped sockets, flapping sites |
| Telemetry | OpenTelemetry collector, Prometheus, Loki, Grafana | One `docker compose up` |
| Triage agent | Gemini API with tools: query Prometheus/Loki, send `TriggerMessage` / `Reset` | Commands need a human click; read-only by default |
| CRA in CI | GitHub Actions: Syft (CycloneDX SBOM), Grype or OSV-Scanner, `SECURITY.md` + CVD policy, signed firmware | Plus a technical-file skeleton generated per release |

## SLOs and incidents to script

- Site availability (stations `Available` or `Charging` / total), heartbeat gaps, session start success rate.
- 10 scripted incidents with known root cause: site network drop, firmware crash loop, RFID backend timeout,
  meter value drift, mass reboot after a bad OTA, a single "stuck" connector, and others.
- Eval: the agent's triage vs ground truth for all 10, published in the README (accuracy, time to summary,
  false "all clear"). This is the part most AI demos skip.

## Plan

| Weekend | Output |
|---|---|
| 1 | Hardware charger on the desk talking OCPP to your CSMS; 50 simulated stations; Grafana showing both |
| 2 | Toxiproxy chaos, SLOs and alerts, triage agent with approval gate, the 10-incident eval |
| 3 | CRA CI pipeline, README with diagram, 2-min video, write-up |

## Write-up

"A SaaS observability playbook for a charger fleet": what carries over from running SaaS, what doesn't (intermittent
connectivity, firmware you can't hot-fix, physical faults), how long it took with agents, and what the agent got wrong.

## Guardrails

- Low voltage only; nothing connects to a real EV or mains.
