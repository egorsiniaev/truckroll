from control.metrics import metrics_text


def test_metrics_cover_sites_money_price_and_incidents():
    snap = {
        "sites": [{"code": "A2-HUB", "power_w": 5.0, "cap_w": 10.0, "cap_used": 0.5, "free": 3, "solar_w": 1.0,
                   "demand": {"queue": 2, "longest_wait_s": 30, "abandoned": 1},
                   "economics": {"tariff_eur_kwh": 0.69, "revenue_eur": 10, "cost_eur": 4, "margin_eur": 6, "lost_eur": 1},
                   "controller": {"active": True}}],
        "prices": {"available": True, "now_eur_mwh": 291.0},
        "incidents": [{"status": "open", "severity": "critical"}, {"status": "recovered", "severity": "warning"}],
        "scenario": {"running": {"id": "x"}},
    }
    text = metrics_text(snap)
    assert 'truckroll_site_queue_cars{site="A2-HUB"} 2.0' in text
    assert 'truckroll_site_margin_eur_today{site="A2-HUB"} 6.0' in text
    assert "truckroll_price_eur_per_mwh 291.0" in text
    assert 'truckroll_incidents_open{severity="critical"} 1.0' in text
    assert 'truckroll_incidents_open{severity="warning"} 0.0' in text
    assert 'truckroll_site_controller_active{site="A2-HUB"} 1.0' in text
    assert "truckroll_scenario_running 1.0" in text
