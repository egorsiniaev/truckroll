"""Prometheus text exposition of the control service's view: load, queues, money, incidents."""

from __future__ import annotations

from collections.abc import Mapping


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def metrics_text(snap: Mapping) -> str:
    lines: list[str] = []

    def gauge(name: str, help_: str, rows: list[tuple[dict, float | None]]) -> None:
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} gauge")
        for labels, value in rows:
            if value is None:
                continue
            lab = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items())
            lines.append(f"{name}{{{lab}}} {float(value)}" if lab else f"{name} {float(value)}")

    sites = snap.get("sites", [])
    site_rows = lambda f: [({"site": s["code"]}, f(s)) for s in sites]  # noqa: E731
    gauge("truckroll_site_power_watts", "Site power draw", site_rows(lambda s: s["power_w"]))
    gauge("truckroll_site_cap_watts", "Site grid connection cap", site_rows(lambda s: s["cap_w"]))
    gauge("truckroll_site_solar_watts", "Live solar at the site", site_rows(lambda s: s.get("solar_w")))
    gauge("truckroll_site_load_ratio", "Power / grid cap", site_rows(lambda s: s.get("cap_used")))
    gauge("truckroll_site_uptime_today", "Time-weighted uptime since local midnight (or service start)",
          site_rows(lambda s: s.get("uptime_today")))
    gauge("truckroll_site_free_connectors", "Connectors free right now", site_rows(lambda s: s.get("free")))
    gauge("truckroll_site_queue_cars", "Cars waiting for a connector",
          site_rows(lambda s: (s.get("demand") or {}).get("queue")))
    gauge("truckroll_site_longest_wait_seconds", "Longest current wait",
          site_rows(lambda s: (s.get("demand") or {}).get("longest_wait_s")))
    gauge("truckroll_site_abandoned_today", "Drivers who gave up waiting today",
          site_rows(lambda s: (s.get("demand") or {}).get("abandoned")))
    gauge("truckroll_site_tariff_eur_per_kwh", "Charging tariff",
          site_rows(lambda s: (s.get("economics") or {}).get("tariff_eur_kwh")))
    for key, help_ in (("revenue_eur", "Revenue today"), ("cost_eur", "Energy cost today"),
                       ("margin_eur", "Margin today"), ("lost_eur", "Revenue lost to waits and interruptions today")):
        gauge(f"truckroll_site_{key}_today", help_, site_rows(lambda s, k=key: (s.get("economics") or {}).get(k)))
    gauge("truckroll_site_controller_active", "1 if the site controller is limiting chargers",
          site_rows(lambda s: int(bool((s.get("controller") or {}).get("active")))))
    prices = snap.get("prices") or {}
    gauge("truckroll_price_eur_per_mwh", "Day-ahead price now (NL)",
          [({}, prices.get("now_eur_mwh"))] if prices.get("available") else [])
    incidents = [i for i in snap.get("incidents", []) if i["status"] != "recovered"]
    gauge("truckroll_incidents_open", "Open or monitoring incidents by severity",
          [({"severity": sev}, sum(1 for i in incidents if i["severity"] == sev)) for sev in ("critical", "warning", "info")])
    gauge("truckroll_scenario_running", "1 while a scenario runs", [({}, int(bool((snap.get("scenario") or {}).get("running"))))])
    return "\n".join(lines) + "\n"
