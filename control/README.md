# control

Sits between the web page and the moving parts. Polls the CSMS admin API and Toxiproxy every 2 s, fetches
Open-Meteo weather every 10 min, and streams one snapshot to every page over SSE. Serves the page from `web/`.

**Local only: no auth yet.** Before anything goes public, the control API needs auth and a read-only public subset.

| Endpoint | Does |
|---|---|
| `GET /api/state`, `GET /api/stream` | Snapshot: KPIs, sites (power, cap, availability, weather, solar, network), stations, events |
| `POST /api/sites/{code}/outage` `{on}` | Cut or restore the site's network (Toxiproxy) |
| `POST /api/sites/{code}/latency` `{ms}` | Add latency to the site's network (0 removes) |
| `POST /api/chaos/clear` | Restore every site's network |
| `POST /api/stations/{id}/limit` `{watts\|null}` | OCPP ChargePointMaxProfile via the CSMS |
| `POST /api/stations/{id}/fault` `{error_code}`, `/clear-fault` | Station reports Faulted / Available (simulator) |
| `POST /api/stations/{id}/power` `{on}`, `/reset` | Switch the simulated station off/on; OCPP soft reset |
| `POST /api/world` `{enabled}` | Real-world mode (on by default): local time sets arrivals, weather and prices set limits |
| `POST /api/autopilot` `{enabled}` | Random scenarios every 5-10 min (on by default) |
| `POST /api/scenarios/{id}/start`, `/api/scenarios/stop` | Scripted incidents; one at a time; stop cleans up |

API docs: http://localhost:8080/api/docs.

## Automatic parts

All run inside the 2 s poll loop:

- **Limit arbiter** (`limits.py`): operator, site controller, cold derate, price rule and grid-operator requests each ask
  for a limit; the lowest wins and is sent as one OCPP ChargePointMaxProfile. Offline stations get it when back.
- **Site controller** (`controller.py`, every 10 s): if a site draws more than its grid cap plus live solar, every
  connected charger there is limited to an equal share. Limits lift after 60 s below 70 % of the cap.
- **Incidents** (`incidents.py`): detect, respond, recover. Faults get a soft reset after 45 s, then 2 min of
  watching; a fault that returns means hardware and a (simulated) truck roll. Site outages are one incident, not 20.
  Stations that drop 3 times in 3 min are a crash loop. Grid-cap and grid-operator events are incidents too. Every
  timeline entry records its actor (`system`, `auto-rule`, later `operator`), which is where human-in-the-loop
  decisions will plug in.
- **Startup cleanup**: on the first poll, restores every site network and clears limits nobody asked for.

## Real-world mode

- Arrivals: each site has a demand profile over the local (Amsterdam) day; the matching share of stations gets
  arrivals, the rest are idle. Re-applied every minute.
- Cold: below 3 °C, DC stations are limited to 75 % (cold batteries charge slower).
- Storm: gusts of 60 km/h or more add 1500 ms latency to that site's network.
- Solar: irradiance x site PV size x 0.8, shown per site; it raises the site controller's allowance.
- Price: ENTSO-E day-ahead (NL, 15-min slots). When the current price is in the day's top 20 %, the depot fleet
  charges at 50 %. Needs `ENTSOE_API_TOKEN` in the gitignored `.env`; without it, prices are just off.
- Daylight: real sunrise/sunset per day; the page shows morning, day, evening and night.

## Scenarios and the incident ledger

Each run is appended to `/data/ledger.jsonl` (volume `control-data`) with its root cause and targets. That file is
the ground truth for the triage-agent eval, so the agent must never be given access to it.

## Known gaps

- State lives in memory: restarting this service drops a running scenario mid-way. Startup cleanup restores networks
  and leftover limits, and the incident engine resets leftover faults, but the ledger gets no record of the cut run.
