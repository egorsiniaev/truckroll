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
| `POST /api/pricing` `{enabled}` | Dynamic pricing: public tariffs follow spikes and dips (on by default) |
| `POST /api/batteries` `{enabled}` | Site batteries (on by default) |
| `POST /api/decisions/{id}` `{choice}` | Answer a pending question: `approve` or `reject` |
| `POST /api/hitl` `{enabled}` | Human approval (on by default); off = every response runs automatically |
| `GET /metrics` | Prometheus: load, queues, tariffs, money, incidents (scraped for Grafana) |
| `POST /api/scenarios/{id}/start`, `/api/scenarios/stop` | Scripted incidents; one at a time; stop cleans up |

API docs: http://localhost:8080/api/docs.

## Demand and money

- **Demand engine** (`demand.py`): cars arrive per site (Poisson; rate = profile x hour of day x tariff effect), each
  needs a set amount of energy, takes a free connector or waits, and leaves when charged. Drivers give up after their
  patience runs out. A grid cap slows charging, sessions last longer, and the queue grows. The simulator's own
  session generator is off; the engine starts and stops transactions through the simulator API.
- **Economics** (`economics.py`): energy bought at the ENTSO-E spot price plus €0.11/kWh fees, solar free, sold at
  each site's tariff (depot €0.32 fleet contract, office €0.39, retail €0.49, highway €0.69). With dynamic pricing,
  60 % of a price spike reaches public tariffs (capped at +60 %), and price-sensitive drivers go elsewhere
  (elasticity 1.2 retail, 1.5 highway). Revenue, cost, margin and revenue lost to waits reset at local midnight.

- **Discounts and the app queue**: below today's average price, 80 % of the dip comes off public tariffs (down to
  -40 %), plus 10 % off while solar covers half the site; cheaper chargers draw extra drivers (up to 1.8x). Public
  drivers check the expected wait in an app (queue / how fast connectors free up) and go elsewhere when it is long
  compared with their patience; counted as "diverted" and as lost revenue.
- **Batteries** (`battery.py`): one per site (depot 400 kWh/200 kW, retail 150/75, hub 600/300, office 100/50).
  Covers load above the grid cap first, stores spare solar, supplies the site in the day's top 20 % of prices, and
  fills from the grid in the cheapest quarter, only within the cap. 10-95 % SoC, 90 % round trip. Its spare power
  counts toward the site controller's allowance; load on the page is grid draw (chargers - solar + battery) vs cap.
- **Restarts**: uptime, today's money, battery charge, the queue (waiting cars, tracked sessions, counters) and
  incidents (open and recent, with pending timers) are saved to `/data/state.json` every minute and on shutdown,
  and restored on start. Timers are stored as wall-clock time, so downtime counts as time passed.

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

## Human in the loop

Risky responses become questions (`decisions.py`): a truck roll, resetting a charger that is still charging, a
firmware rollback, free vend, taking a charger out of service, restarting a backend worker, a meter calibration
visit. Each has a question, a reason, a default and a deadline (30-90 s). The page shows it as a callout on the map
and at the top of the incidents strip. Approve, reject, or let the deadline pass: the default applies and the
timeline says "no answer in time". Cheap, reversible steps stay automatic (status request, first soft reset,
billing hold). The agent will plug into the same place: it proposes, a person decides.

## Service levels (`slo.py`)

Since local midnight, saved across restarts: uptime as experienced and excluding injected faults (connector-time
at sites and chargers a running scenario affects), session start success (target 98 %), grid cap compliance
(episodes over the cap for more than 60 s, target 0), flex delivery during grid-operator requests (target 95 %).
Shown on the page when nothing is selected, and in Grafana.

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

- Restarting this service still drops a running scenario mid-way (no ledger record of the cut run).
