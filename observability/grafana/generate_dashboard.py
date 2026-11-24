"""Generate the Truckroll Grafana dashboard. Colour code everywhere: green good, orange warning, red problem.

Run: python3 observability/grafana/generate_dashboard.py  (stdlib only; output is committed)
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).parent / "dashboards" / "fleet.json"
P = {"type": "prometheus", "uid": "prometheus"}
L = {"type": "loki", "uid": "loki"}
GREEN, YELLOW, ORANGE, RED, BLUE = "green", "#eab308", "orange", "red", "blue"
HEALTHY = "Available|Preparing|Charging|SuspendedEV|SuspendedEVSE|Finishing"
# Aggregate before joining: after a CSMS restart Prometheus briefly holds duplicate series, and a raw join fails.
AVAIL = (f'max by (site, station, connector) (csms_connector_status{{status=~"{HEALTHY}"}})'
         ' * on(station) group_left() max by (station) (csms_station_connected)')
TOTAL = 'max by (site, station, connector) (csms_connector_status{status="Available"})'


def steps(*pairs):
    """steps(GREEN, (0.3, YELLOW), (0.7, RED)) -> Grafana threshold steps."""
    first, *rest = pairs
    return {"mode": "absolute", "steps": [{"color": first, "value": None}] + [{"color": c, "value": v} for v, c in rest]}


LOAD = steps(GREEN, (0.3, YELLOW), (0.7, RED))
UPTIME = steps(RED, (0.9, ORANGE), (0.97, GREEN))
QUEUE = steps(GREEN, (1, ORANGE), (5, RED))
ZERO_GOOD = steps(GREEN, (1, RED))
PRICE = steps(GREEN, (150, ORANGE), (250, RED))
MONEY = steps(RED, (0, GREEN))
HEARTBEAT = steps(GREEN, (90, ORANGE), (180, RED))

panels: list[dict] = []


def panel(kind, title, exprs, x, y, w, h, unit=None, thresholds=None, ds=P, desc=None, options=None, color_by_threshold=False,
          minv=None, maxv=None):
    defaults: dict = {}
    if unit:
        defaults["unit"] = unit
    if thresholds:
        defaults["thresholds"] = thresholds
        defaults["color"] = {"mode": "thresholds"}
    if color_by_threshold:
        defaults["custom"] = {"thresholdsStyle": {"mode": "line+area"}, "fillOpacity": 10}
    if minv is not None:
        defaults["min"] = minv
    if maxv is not None:
        defaults["max"] = maxv
    p = {
        "id": len(panels) + 1, "type": kind, "title": title, "datasource": ds,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"refId": chr(65 + i), "datasource": ds, "expr": e, **({"legendFormat": lf} if lf else {})}
                    for i, (e, lf) in enumerate(exprs)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": options or {},
    }
    if desc:
        p["description"] = desc
    panels.append(p)


STAT = {"colorMode": "background", "graphMode": "area", "reduceOptions": {"calcs": ["lastNotNull"]}}

# Row 1: is everything OK right now?
panel("stat", "Uptime", [(f"sum({AVAIL}) / count({TOTAL})", None)], 0, 0, 4, 4, "percentunit", UPTIME, options=STAT,
      desc="Working connectors (free or in use, on a connected station) / all connectors.")
panel("stat", "Critical incidents", [('truckroll_incidents_open{severity="critical"}', None)], 4, 0, 4, 4, None, ZERO_GOOD,
      options=STAT)
panel("stat", "Cars waiting", [("sum(truckroll_site_queue_cars)", None)], 8, 0, 4, 4, None, QUEUE, options=STAT)
panel("stat", "Fleet load vs grid caps", [("sum(truckroll_site_power_watts) / sum(truckroll_site_cap_watts)", None)],
      12, 0, 4, 4, "percentunit", LOAD, options=STAT)
panel("stat", "Power price now", [("truckroll_price_eur_per_mwh", None)], 16, 0, 4, 4, "currencyEUR", PRICE, options=STAT,
      desc="ENTSO-E day-ahead, NL, per MWh.")
panel("stat", "Margin today", [("sum(truckroll_site_margin_eur_today)", None)], 20, 0, 4, 4, "currencyEUR", MONEY, options=STAT)

# Row 2: load and queues per site
panel("bargauge", "Load by site now", [("truckroll_site_load_ratio", "{{site}}")], 0, 4, 8, 8, "percentunit", LOAD,
      options={"orientation": "horizontal", "displayMode": "gradient", "reduceOptions": {"calcs": ["lastNotNull"]}},
      minv=0, maxv=1.2, desc="Below 30 % green, 30-70 % yellow, 70 % and up red.")
panel("timeseries", "Load by site (power / grid cap)", [("truckroll_site_load_ratio", "{{site}}")], 8, 4, 8, 8,
      "percentunit", LOAD, color_by_threshold=True)
panel("timeseries", "Cars waiting by site", [("truckroll_site_queue_cars", "{{site}}")], 16, 4, 8, 8, None, QUEUE,
      color_by_threshold=True)

# Row 3: availability and power
panel("timeseries", "Uptime by site", [(f"sum by (site) ({AVAIL}) / count by (site) ({TOTAL})", "{{site}}")],
      0, 12, 8, 8, "percentunit", UPTIME, color_by_threshold=True, minv=0, maxv=1)
panel("timeseries", "Power vs cap by site", [("truckroll_site_power_watts", "{{site}} power"),
                                             ("truckroll_site_cap_watts", "{{site}} cap"),
                                             ("truckroll_site_solar_watts", "{{site}} solar")], 8, 12, 8, 8, "watt")
panel("timeseries", "Oldest heartbeat", [("max by (site) (csms_station_heartbeat_age_seconds)", "{{site}}")],
      16, 12, 8, 8, "s", HEARTBEAT, color_by_threshold=True)

# Row 4: money and market
panel("bargauge", "Margin today by site", [("truckroll_site_margin_eur_today", "{{site}}")], 0, 20, 8, 8, "currencyEUR",
      MONEY, options={"orientation": "horizontal", "displayMode": "basic", "reduceOptions": {"calcs": ["lastNotNull"]}})
panel("timeseries", "Tariff vs energy price", [("truckroll_site_tariff_eur_per_kwh", "{{site}} tariff €/kWh"),
                                               ("truckroll_price_eur_per_mwh / 1000 + 0.11", "energy cost €/kWh")],
      8, 20, 8, 8, "currencyEUR")
panel("timeseries", "Lost revenue today (waits, interruptions)", [("truckroll_site_lost_eur_today", "{{site}}")],
      16, 20, 8, 8, "currencyEUR", steps(GREEN, (5, ORANGE), (25, RED)), color_by_threshold=True)

# Row 5: protocol and logs
panel("timeseries", "Faulted connectors", [('sum by (site) (csms_connector_status{status="Faulted"})', "{{site}}")],
      0, 28, 8, 8, None, ZERO_GOOD, color_by_threshold=True)
panel("timeseries", "OCPP messages/s by action", [("sum by (action) (rate(ocpp_messages_total[1m]))", "{{action}}")],
      8, 28, 8, 8, "reqps")
panel("logs", "CSMS warnings", [('{service_name="csms"} |~ "WARN|rejected|Faulted|unknown"', None)], 16, 28, 8, 8, ds=L)

dashboard = {"uid": "truckroll-fleet", "title": "Truckroll fleet", "tags": ["truckroll"], "timezone": "browser",
             "schemaVersion": 41, "refresh": "15s", "time": {"from": "now-1h", "to": "now"}, "panels": panels}

if __name__ == "__main__":
    OUT.write_text(json.dumps(dashboard, indent=2) + "\n")
    print(f"{len(panels)} panels -> {OUT}")
