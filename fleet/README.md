# fleet

Simulated charging network on the [SAP e-mobility charging stations simulator](https://github.com/SAP/e-mobility-charging-stations-simulator)
(built from tag `v5.0`). Every site connects through its own Toxiproxy port, so chaos can hit one site at a time.

| Site | Kind | Stations x connectors | Power | Proxy port |
|---|---|---|---|---|
| `AMS-DEPOT` | logistics depot | 20 x 1 | AC 22 kW | 8001 |
| `UTR-RETAIL` | retail parking | 12 x 2 | AC 22 kW | 8002 |
| `A2-HUB` | highway fast-charging hub | 8 x 2 | DC 150 kW | 8003 |
| `EHV-OFFICE` | office campus | 10 x 1 | AC 11 kW | 8004 |

50 stations, 70 connectors. Sessions come from the simulator's automatic transaction generator for now; week 2
replaces that with arrivals driven by real time and weather.

All files here are generated from the site table in `generate.py`. Edit the table, then:

```bash
python3 fleet/generate.py
```
