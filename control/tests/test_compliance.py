import random
from datetime import datetime

from control.compliance import daily
from control.disruption import OutagePlanner
from control.firmware import TZ

DAY = "2026-11-30"


class Runner:
    def __init__(self):
        self.current = None
        self.run_id = ""
        self.started = []

    def start(self, scenario_id, stations):
        self.current = scenario_id
        self.run_id = f"{scenario_id}-1"
        self.started.append(scenario_id)


def test_area_outage_runs_once_a_day_and_records_the_loss():
    t = [datetime(2026, 11, 30, 8, 0, tzinfo=TZ).timestamp()]
    lost = {"UTR-RETAIL": 10.0}
    runner = Runner()
    p = OutagePlanner(["UTR-RETAIL"], runner, lambda code: lost[code], rng=random.Random(2), clock=lambda: t[0])
    assert p.tick([{"id": "x"}]) is None
    assert 9 <= datetime.fromtimestamp(p.plan["at"], TZ).hour < 19
    t[0] = p.plan["at"] + 1
    assert "down for 30 min" in p.tick([{"id": "x"}])
    assert runner.started == ["area-outage-utr-retail"]
    lost["UTR-RETAIL"] = 95.5
    t[0] += 1830
    runner.current = None
    assert "€86" in p.tick([{"id": "x"}])
    assert p.for_day(DAY)["loss_eur"] == 85.5 and p.for_day(DAY)["minutes"] == 30.5
    t[0] += 3600
    assert p.tick([{"id": "x"}]) is None and len(runner.started) == 1  # once a day


def test_outage_waits_for_a_running_scenario():
    t = [datetime(2026, 11, 30, 12, 0, tzinfo=TZ).timestamp()]
    runner = Runner()
    runner.current = "storm-a2"
    p = OutagePlanner(["A2-HUB"], runner, lambda code: 0.0, rng=random.Random(1), clock=lambda: t[0])
    p.tick([{"id": "x"}])
    t[0] = p.plan["at"] + 1
    assert p.tick([{"id": "x"}]) is None and not runner.started


def outage_incident():
    return {"id": "INC-0007", "kind": "site_outage", "site": "UTR-RETAIL", "title": "UTR-RETAIL offline",
            "opened": "2026-11-30T13:00:20+01:00", "status": "recovered", "ttr_s": 1790, "metrics": {"ttd_s": 15},
            "timeline": []}


def test_daily_report_flags_the_outage_and_an_exploited_vulnerability():
    outage = {"day": DAY, "site": "UTR-RETAIL", "started": "2026-11-30T13:00:00+01:00",
              "ended": "2026-11-30T13:30:30+01:00", "minutes": 30.5, "loss_eur": 85.5}
    events = [
        {"time": "2026-11-30T14:00:00+01:00", "type": "release", "version": "2.8.3", "bump": "patch", "notes": "x",
         "security": "high", "advisory": "TRSA-2026-004", "exploited": True},
        {"time": "2026-11-30T14:03:00+01:00", "type": "installed", "station": "S-1", "to": "2.8.3", "kind": "patch",
         "minutes": 2.0},
        {"time": "2026-11-30T14:09:00+01:00", "type": "installed", "station": "S-2", "to": "2.8.3", "kind": "patch",
         "minutes": 2.5},
        {"time": "2026-11-30T14:10:00+01:00", "type": "install_failed", "station": "S-3", "to": "2.8.3"},
        {"time": "2026-11-30T14:12:00+01:00", "type": "rolled_back", "station": "S-3", "to": "2.8.2"},
    ]
    releases = [{"version": "2.8.3", "security": "high", "status": "rolling"}]
    versions = {"S-1": "2.8.3", "S-2": "2.8.3", "S-3": "2.8.2"}
    money = {"fleet": {"lost_eur": 140.0, "service_eur": 0, "revenue_eur": 900.0, "margin_eur": 300.0}}
    rep = daily(DAY, [outage_incident()], events, outage, versions, releases, money, 0.97, "now", False)

    n, c = rep["nis2"], rep["cra"]
    assert n["verdict"] == "reportable"
    assert n["significant"][0]["id"] == "INC-0007"  # 1790 s + 15 s to detect = over 30 min
    assert n["area_outage"]["incident_id"] == "INC-0007" and n["financial"]["outage_loss_eur"] == 85.5
    assert n["patching"]["installs_failed"] == 1 and n["patching"]["rollbacks"] == 1
    assert c["verdict"] == "reportable" and c["exploited"][0]["advisory"] == "TRSA-2026-004"
    assert c["security_fixes"][0]["median_min"] == 6.0
    assert c["fleet"]["exposed"] == 1 and c["fleet"]["exposed_stations"] == ["S-3"]
    assert rep["headline"].startswith("NIS2: reportable")


def test_quiet_day_has_nothing_to_report():
    rep = daily(DAY, [], [], None, {"S-1": "1.0.0"}, [], None, None, "now", True)
    assert rep["nis2"]["verdict"] == "nothing to report" and rep["cra"]["verdict"] == "nothing to report"
