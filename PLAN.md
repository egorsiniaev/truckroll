# Plan

Goals: [GOALS.md](GOALS.md). Original spec: [SPEC.md](SPEC.md) (this plan supersedes its weekend plan).

## Architecture

```
HOME (outbound only, no open ports)          HETZNER (public via Caddy, TLS)
-----------------------------------          -----------------------------------------------
[ESP32 charger + camera] --OCPP WSS----->   [CSMS] <--toxiproxy-- [SAP simulator: 3-5 sites, ~50 stations]
        |                (TLS + auth)          |   ^
        +--- camera stream (push) ------->   [stream relay]       [site controller] <-- [real feeds]
                                               |   |                caps, solar, prices    weather, prices,
                                               v   v                                       grid load, time
                                   [OTel -> Prometheus / Loki / Grafana]
                                               |
                                    alerts --> [triage agent] --proposed fix--> [approval gate]
                                               |
                                   [incident engine + hidden ledger]  <-- game days, real-world triggers, visitors
                                               |
                                   [public read-only API] --> siniaev.dev page (live view, "be on call")
```

- Hetzner runs everything except the device. The device dials out; home stays closed.
- Grafana and admin APIs stay private (Tailscale/WireGuard). The public gets a read-only API plus a
  rate-limited, fixed-menu incident trigger.
- Secrets (ENTSO-E token, Gemini key, OCPP passwords) from env vars on the server, never in git.

## The device (make it worth watching)

- Lamp = the car; brightness follows the charging limit the site controller sets.
- WS2812 ring: Available / Charging / Faulted, plus a "grid stress" colour when a congestion request is active.
- OLED: power, kWh, current price, grid cap, connection state.
- Temperature sensor: a hairdryer causes a real derate.
- Fault button and Wi-Fi pull for live incidents.
- Camera points only at the device. Stream pushes out to the Hetzner relay; nothing else in frame.

## Incidents

Every injected incident goes into a ledger with its root cause. The agent can't see the ledger; the eval compares
the agent's verdict against it.

| # | Incident | Injection |
|---|---|---|
| 1 | Site network drop | Toxiproxy cut |
| 2 | Flapping site | Toxiproxy intermittent resets |
| 3 | Crash loop after bad OTA | stations reboot on a loop |
| 4 | RFID auth backend timeout | CSMS auth latency |
| 5 | Meter value drift | skewed MeterValues |
| 6 | Stuck connector | Charging status, zero energy |
| 7 | Overheating derate | weather (heatwave) or hairdryer on the device |
| 8 | Charger ignores charging profile | station keeps drawing over the limit |
| 9 | CSMS worker crash / backlog | kill worker, queue grows |
| 10 | Reconnect storm after backend restart | all stations reconnect at once |
| D1-D2 | Decoys: expected derate, normal price-driven shedding | tests false positives |

Sources: scheduled game days (labelled, for the eval), real-world triggers (wind gust -> site drop, price spike
-> shedding), and visitors on the page.

## Real-world feeds

| Feed | Drives |
|---|---|
| Local time | Arrival patterns |
| Open-Meteo (no key) | Solar output, heat derate, storm gusts |
| ENTSO-E day-ahead prices (token) | Charge timing, price-spike shedding |
| ENTSO-E actual load / wind | Simulated grid operator congestion requests |

## Public page visual

Reference style: an isometric, low-poly site scene in soft whites (a depot with solar roof, a solar carport,
chargers with vans, battery storage, a grid pylon), with animated dashed lines for power flow, floating labels with
live bars, and a row of round gauges under an "Energy Management System" heading. Use our own art; the reference
is for style only (not committed).

- One scene per site, driven by the live API: flow lines speed up or reverse with power, chargers glow by status,
  faulted ones turn red, the grid line pulses during congestion, the sky follows real weather and time of day.
- Gauges: solar now, grid cap used, charging load (battery only if we simulate one).
- Clicking a charger or site opens its state and open incidents; the incident trigger lives here too.
- Build: SVG + CSS/JS animation first (cheap); Three.js only if time is left.
- Risk: this is the most likely overrun in week 5. Fallback: a flat 2D version of the same scene.

## Milestones

Hours are estimates; actuals go in [LOG.md](LOG.md).

| Week | Dates | Output | Est. h | Done when |
|---|---|---|---|---|
| 0 | now | Order the hardware (BOM in SPEC.md + camera + temp sensor) | 1 | parts ordered |
| 1 | Nov 23-29 | CSMS + simulator + telemetry stack on Hetzner | 10 | 50 stations in Grafana, heartbeats and sessions flowing |
| 2 | Nov 30-Dec 6 | Real feeds + site controller with caps and smart charging | 7 | a real price spike cuts site load, cap never exceeded |
| 3 | Dec 7-13 | Incident engine + ledger + 10 incidents + SLO alerts | 6 | each incident fires the right alert |
| 3-4 | when parts arrive | Device: MicroOcpp, lamp, ring, OLED, camera | 5 | setting 50% in the controller dims the lamp, live on camera |
| 4 | Dec 14-20 | Triage agent + approval gate + eval run | 8 | eval table with real numbers |
| 5 | Dec 21-27 | Public page on siniaev.dev, video, write-up | 6 | a stranger can trigger an incident and approve the fix |
| 6 | Dec 28-31 | Buffer, make the repo public (ask first) | - | page live, numbers published |

Total ~43 h. If over budget, cut in this order: live visitor trigger -> replay of a recorded day; camera ->
recorded clips; site battery and upstream PRs were never in.

## Risks

- **Parts shipping late:** software runs first; the device slots in when parts arrive.
- **Public page abuse:** fixed incident menu, rate limits, daily Gemini API spend cap, replay fallback.
- **Agent cost:** cap tokens per incident; log cost per triage.
- **Scope creep from the energy side:** one controller, caps + prices + solar only.
