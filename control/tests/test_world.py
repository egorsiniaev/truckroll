
from datetime import date, datetime

from control.limits import LimitArbiter
from control.world import (
    COLD_DERATE,
    FLAT_SHARE,
    STORM_LATENCY_MS,
    TZ,
    WorldEngine,
    day_type,
    demand_share,
    nl_holidays,
    solar_w,
    weather_rules,
)

HUB = {"code": "A2-HUB", "current": "DC", "power_w": 150000, "proxy": "a2-hub", "pv_w": 80000, "demand": "highway"}
DEPOT = {"code": "AMS-DEPOT", "current": "AC", "power_w": 22080, "proxy": "ams-depot", "pv_w": 150000, "demand": "depot"}


def test_demand_profiles_follow_the_local_day():
    assert demand_share("office", 3) == 0.0
    assert demand_share("office", 8) == 0.8
    assert demand_share("depot", 23) == 0.9
    assert demand_share("unknown", 12) == 1.0


def test_weekends_are_not_weekdays():
    assert day_type(date(2026, 11, 28)) == ("saturday", None)
    assert day_type(date(2026, 11, 29)) == ("sunday", None)
    assert day_type(date(2026, 11, 30)) == ("weekday", None)
    assert demand_share("office", 8, "saturday") < 0.1  # nobody comes to the office
    assert demand_share("retail", 12, "saturday") > demand_share("retail", 12, "weekday")  # Saturday shopping peak
    assert demand_share("retail", 10, "sunday") < 0.1  # Dutch shops open at noon on Sunday
    assert demand_share("highway", 8, "sunday") < demand_share("highway", 8, "weekday")  # no commute
    assert demand_share("depot", 12, "sunday") > demand_share("depot", 12, "weekday")  # vans parked


def test_dutch_holidays_run_like_sundays():
    days = nl_holidays(2026)
    assert days[date(2026, 4, 5)] == "Easter"
    assert days[date(2026, 4, 27)] == "King's Day"
    assert days[date(2026, 5, 14)] == "Ascension Day"
    assert nl_holidays(2025)[date(2025, 4, 26)] == "King's Day"  # 27 Apr 2025 is a Sunday
    assert day_type(date(2026, 12, 25)) == ("sunday", "Christmas")


def test_engine_uses_the_kind_of_day():
    office = {"code": "EHV-OFFICE", "demand": "office"}
    saturday = WorldEngine([office], None, None, enabled=True, clock=lambda: datetime(2026, 11, 28, 8, 30, tzinfo=TZ))
    monday = WorldEngine([office], None, None, enabled=True, clock=lambda: datetime(2026, 11, 30, 8, 30, tzinfo=TZ))
    assert saturday.shares()["EHV-OFFICE"] < 0.1 < monday.shares()["EHV-OFFICE"]
    assert "weekend" in saturday.reasons()["EHV-OFFICE"]
    assert saturday.status()["day_type"] == "saturday"


def test_solar_scales_with_irradiance():
    assert solar_w(DEPOT, {"radiation_wm2": 500}) == 150000 * 0.5 * 0.8
    assert solar_w(DEPOT, None) == 0.0


def test_cold_rule_only_for_dc_and_storm_rule_by_gusts():
    cold_hub = {r.rule: r.active for r in weather_rules(HUB, {"temp_c": 1.0, "gust_kmh": 70})}
    cold_depot = {r.rule: r.active for r in weather_rules(DEPOT, {"temp_c": 1.0, "gust_kmh": 20})}
    assert cold_hub == {"cold_derate": True, "storm_network": True}
    assert cold_depot == {"storm_network": False}  # AC site: the DC rule doesn't apply


class Recorder:
    def __init__(self):
        self.calls = []

    async def set_atg_many(self, ids, running):
        self.calls.append(("atg", tuple(ids), running))

    async def set_limit(self, sid, watts):
        self.calls.append(("limit", sid, watts))

    async def set_latency(self, proxy, ms):
        self.calls.append(("latency", proxy, ms))


class Clients:
    def __init__(self):
        self.simulator = self.csms = self.toxiproxy = Recorder()


def engine(sites, clients, hour=12):
    monday = datetime(2026, 11, 30, hour, tzinfo=TZ)
    return WorldEngine(sites, clients, LimitArbiter(clients.csms), clock=lambda: monday)


def snap(site, n):
    return {"stations": [{"id": f"{site}-{i}", "site": site, "connected": True} for i in range(1, n + 1)]}


async def test_disabled_engine_only_evaluates_rules():
    clients = Clients()
    eng = engine([HUB], clients)
    assert await eng.tick(snap("A2-HUB", 2), {"A2-HUB": {"temp_c": 0, "gust_kmh": 80}}) == []
    assert clients.simulator.calls == []
    assert any(r.active for r in eng.rules)


async def test_enabled_engine_derates_cold_dc_and_degrades_stormy_network_then_undoes(monkeypatch):
    monkeypatch.setattr("control.world.local_hour", lambda now=None: 17)  # highway profile 0.9
    clients = Clients()
    eng = engine([HUB], clients)
    eng.enabled = True
    changes = await eng.tick(snap("A2-HUB", 2), {"A2-HUB": {"temp_c": 0, "gust_kmh": 80}})

    calls = clients.simulator.calls
    assert ("limit", "A2-HUB-1", round(150000 * COLD_DERATE)) in calls
    assert ("latency", "a2-hub", STORM_LATENCY_MS) in calls
    assert any("cold" in c for c in changes) and any("storm" in c for c in changes)

    calls.clear()
    await eng.disable()
    assert ("limit", "A2-HUB-1", None) in calls and ("latency", "a2-hub", 0) in calls


def test_shares_follow_the_day_only_in_real_world_mode():
    office = {**DEPOT, "code": "EHV-OFFICE", "demand": "office"}
    eng = engine([office], Clients(), hour=3)
    assert eng.shares() == {"EHV-OFFICE": FLAT_SHARE}
    eng.enabled = True
    assert eng.shares() == {"EHV-OFFICE": 0.0}


def test_price_rule_only_for_depot_and_when_expensive():
    from control.world import price_rule
    assert price_rule(HUB, {"available": True}) is None
    hot = price_rule(DEPOT, {"available": True, "now_eur_mwh": 210.0, "expensive": True, "expensive_from_eur_mwh": 150})
    calm = price_rule(DEPOT, {"available": True, "now_eur_mwh": 80.0, "expensive": False, "expensive_from_eur_mwh": 150})
    assert hot.active and "top 20%" in hot.text
    assert not calm.active and not price_rule(DEPOT, None).active


def test_demand_reasons_explain_the_hour():
    eng = engine([HUB], Clients(), hour=17)
    assert eng.reasons() == {"A2-HUB": "test demand (real-world mode off)"}
    eng.enabled = True
    assert eng.reasons() == {"A2-HUB": "evening rush hour"}
