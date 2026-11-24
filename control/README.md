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
| `POST /api/world` `{enabled}` | Real-world mode: local time sets arrivals per site, weather sets derates and network trouble |
| `POST /api/scenarios/{id}/start`, `/api/scenarios/stop` | Scripted incidents; one at a time; stop cleans up |

API docs: http://localhost:8080/api/docs.

## Real-world mode

- Arrivals: each site has a demand profile over the local (Amsterdam) day; the matching share of stations gets
  arrivals, the rest are idle. Re-applied every minute.
- Cold: below 3 °C, DC stations are limited to 75 % (cold batteries charge slower).
- Storm: gusts of 60 km/h or more add 1500 ms latency to that site's network.
- Solar: irradiance x site PV size x 0.8, shown per site.

## Scenarios and the incident ledger

Each run is appended to `/data/ledger.jsonl` (volume `control-data`) with its root cause and targets. That file is
the ground truth for the triage-agent eval, so the agent must never be given access to it.

## Known gaps

- State lives in memory: restarting this service drops a running scenario without cleanup and turns real-world
  mode off without undoing its limits. Use `POST /api/chaos/clear` and clear limits/faults after a restart.
