# csms

Thin OCPP 1.6J backend on [mobilityhouse/ocpp](https://github.com/mobilityhouse/ocpp). Stations connect to
`ws://<host>:9000/<station_id>` with subprotocol `ocpp1.6`. The site is the station id without its trailing number
(`AMS-DEPOT-00007` -> `AMS-DEPOT`).

Exports metrics and logs over OTLP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set.

| Env | Default | Meaning |
|---|---|---|
| `CSMS_PORT` | 9000 | WebSocket port; `GET /healthz` returns 200 |
| `CSMS_HEARTBEAT_INTERVAL_S` | 30 | Interval sent in BootNotification |
| `CSMS_ID_TAGS_FILE` | unset | JSON array of accepted RFID tags |
| `CSMS_ACCEPT_ANY_TAG` | false | Accept every tag (dev only) |

```bash
uv run pytest --cov=csms      # tests
uv run python -m csms.main    # run locally
```
