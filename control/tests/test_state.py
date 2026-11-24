from control.state import build_snapshot, detect_events, station_state

SITE = {"code": "A2-HUB", "kind": "hub", "stations": 2, "connectors": 2, "power_w": 150000, "current": "DC",
        "lat": 52.0, "lon": 5.0, "cap_w": 300000, "proxy": "a2-hub"}


def st(sid, connected=True, statuses=("Available",), power=0.0, errors=None):
    return {"id": sid, "site": "A2-HUB", "connected": connected,
            "connectors": [{"id": i + 1, "status": s, "error_code": (errors or {}).get(i, "NoError"),
                            "power_w": power, "energy_wh": 0, "transaction_id": None} for i, s in enumerate(statuses)]}


def test_station_state_priority():
    assert station_state(st("x", connected=False, statuses=("Charging",))) == "offline"
    assert station_state(st("x", statuses=("Charging", "Faulted"))) == "faulted"
    assert station_state(st("x", statuses=("Available", "SuspendedEV"))) == "charging"
    assert station_state(st("x", statuses=("Finishing",))) == "preparing"
    assert station_state(st("x", statuses=("Available",))) == "available"
    assert station_state(st("x", statuses=("Unavailable",))) == "unavailable"


def test_snapshot_kpis_and_site_summary():
    stations = [st("A2-HUB-00001", statuses=("Charging", "Available"), power=50000.0),
                st("A2-HUB-00002", connected=False, statuses=("Available", "Available"))]
    proxies = {"a2-hub": {"enabled": False, "toxics": [{"type": "latency", "attributes": {"latency": 800}}]}}
    snap = build_snapshot([SITE], stations, proxies, {"A2-HUB": {"temp_c": 4}}, {}, "now")

    assert snap["kpis"]["stations_online"] == 1
    assert snap["kpis"]["charging"] == 1
    assert snap["kpis"]["availability"] == 0.5
    assert snap["kpis"]["power_w"] == 100000.0
    site = snap["sites"][0]
    assert site["network"] == {"enabled": False, "latency_ms": 800, "known": True}
    assert site["cap_used"] == round(100000 / 300000, 4)
    assert site["states"] == {"charging": 1, "offline": 1}
    assert site["weather"] == {"temp_c": 4}


def test_snapshot_without_csms_data_still_lists_sites():
    snap = build_snapshot([SITE], [], {}, {}, {"csms": "down"}, "now")
    assert snap["sites"][0]["availability"] == 0.0
    assert snap["kpis"]["availability"] is None
    assert snap["upstream_errors"] == {"csms": "down"}


def test_events_for_state_changes():
    before = [{"id": "a", "site": "S", "state": "available", "connectors": []},
              {"id": "b", "site": "S", "state": "offline", "connectors": []},
              {"id": "c", "site": "S", "state": "charging", "connectors": []},
              {"id": "d", "site": "S", "state": "faulted", "connectors": []}]
    after = [{"id": "a", "site": "S", "state": "offline", "connectors": []},
             {"id": "b", "site": "S", "state": "available", "connectors": []},
             {"id": "c", "site": "S", "state": "faulted",
              "connectors": [{"status": "Faulted", "error_code": "HighTemperature"}]},
             {"id": "d", "site": "S", "state": "available", "connectors": []}]
    texts = {e["station"]: e["text"] for e in detect_events(before, after)}
    assert texts == {"a": "went offline", "b": "back online", "c": "faulted (HighTemperature)", "d": "fault cleared"}


def test_bursts_are_grouped_per_site_but_small_changes_stay_individual():
    from control.state import group_events
    burst = [{"level": "error", "station": f"S-{i}", "site": "S", "text": "went offline"} for i in range(5)]
    single = [{"level": "error", "station": "T-1", "site": "T", "text": "went offline"}]
    out = group_events(burst + single)
    assert {"level": "error", "station": None, "site": "S", "text": "5 stations went offline"} in out
    assert single[0] in out and len(out) == 2
