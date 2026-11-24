# observability

CSMS -> OTLP -> OpenTelemetry collector -> Prometheus (metrics) and Loki (logs) -> Grafana.

- Grafana: http://localhost:3000, dashboard **Truckroll fleet** (anonymous admin, localhost only).
- Prometheus: http://localhost:9090.

Key metrics (Prometheus names):

| Metric | Meaning |
|---|---|
| `csms_station_connected` | 1 if the station has an open socket |
| `csms_station_heartbeat_age_seconds` | Seconds since the station last sent anything |
| `csms_connector_status{status}` | 1 for the connector's current OCPP status, 0 for every other status |
| `csms_connector_error{error_code}` | Same, for OCPP error codes |
| `csms_connector_power_watts`, `csms_connector_energy_wh` | Last meter values |
| `csms_sessions_started_total`, `_rejected_total`, `_stopped_total` | Session counters per site |
| `ocpp_messages_total{action}` | OCPP calls received |

Fleet availability = connectors on a connected station in a usable state (Available, Preparing, Charging,
Suspended*, Finishing) / all connectors.
