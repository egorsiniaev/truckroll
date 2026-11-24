from datetime import UTC, datetime

from control.uptime import UptimeTracker


def st(sid, site, connected, *statuses):
    return {"id": sid, "site": site, "connected": connected, "connectors": [{"status": s} for s in statuses]}


def test_time_weighted_uptime_counts_faults_and_offline_as_downtime():
    up = UptimeTracker(["S"])
    t = datetime(2026, 11, 24, 12, tzinfo=UTC)
    up.accrue([st("S-1", "S", True, "Charging", "Available")], 60, t)   # 2 connectors up for 60 s
    up.accrue([st("S-1", "S", True, "Faulted", "Available")], 60, t)    # 1 up, 1 down
    up.accrue([st("S-1", "S", False, "Available", "Available")], 60, t)  # offline: both down
    s = up.summary()
    assert s["sites"]["S"] == round(3 / 6, 5)
    assert s["fleet"] == round(3 / 6, 5)
    assert s["connector_downtime_min"]["S"] == 3.0
    assert s["since"] == "13:00"  # started mid-day: says so


def test_resets_at_local_midnight():
    up = UptimeTracker(["S"])
    up.accrue([st("S-1", "S", False, "Available")], 60, datetime(2026, 11, 24, 22, tzinfo=UTC))
    up.accrue([st("S-1", "S", True, "Available")], 60, datetime(2026, 11, 24, 23, 30, tzinfo=UTC))
    assert up.summary()["sites"]["S"] == 1.0 and up.summary()["since"] == "00:00"
