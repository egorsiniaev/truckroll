# Goals

Deadline: 2026-12-31. Budget: 40 hours to start, logged in [LOG.md](LOG.md).

## 1. Learn the domain

Hold a deep conversation about charging and site energy with people who run it.

- OCPP 1.6J core flows, debugged from my own logs: Boot, Heartbeat, Authorize, Start/StopTransaction,
  MeterValues, StatusNotification, SetChargingProfile.
- OCPP 2.0.1 deltas (device model, security profiles), OCPI roaming, CPO vs eMSP, IEC 61851 control pilot,
  ISO 15118 / Plug & Charge: working knowledge.
- Site energy: grid connection caps, congestion management, day-ahead prices, solar, flexible load.
- Regulation that drives buying: uptime rules (NEVI 97%, AFIR), NIS2 for operators, CRA (24h reporting of
  actively exploited vulnerabilities, in force since 2026-09).

**Done when:** I can write one page on "why chargers and sites fail in the field" from memory.

## 2. Show real, measured impact

Numbers, not just a demo. Published even when they are bad.

| Metric | Target |
|---|---|
| Site availability (Available or Charging / total) | >= 99% over the run |
| Session start success | >= 98% |
| Grid cap compliance: site load over cap for > 60 s | 0 times |
| Flex delivery: delivered / requested kW on congestion requests | >= 95% |
| Agent triage, correct root cause (top-1) | >= 80% |
| Agent false "all clear" | 0 |
| Median time from alert to agent summary | < 2 min |
| Hours spent, agent mistakes, manual fixes | logged, published |

Stretch: 1-2 merged upstream PRs (MicroOcpp, SAP simulator, or mobilityhouse/ocpp).

## 3. Showcase

One public page at siniaev.dev that people want to play with.

- Live: sites on a map, grid cap vs load, real prices and weather, open incidents, a webcam on the real device.
- Interactive: a visitor triggers an incident, watches the alert and the agent's triage, and approves or
  rejects the fix. The human gate is the game.
- Story: built it, ran it, broke it, measured it. Video, numbers, write-up, and what went wrong.

## Non-goals

- A production CSMS or a product.
- Anything on mains or a real EV.
- OCPP 2.0.1 on the device, batteries, OCPI (stretch only).
