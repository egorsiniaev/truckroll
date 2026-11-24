import pytest

from control.limits import LimitArbiter
from control.world import (COLD_DERATE, STORM_LATENCY_MS, WorldEngine, active_stations, demand_share, solar_w,
                           weather_rules)

HUB = {"code": "A2-HUB", "current": "DC", "power_w": 150000, "proxy": "a2-hub", "pv_w": 80000, "demand": "highway"}
DEPOT = {"code": "AMS-DEPOT", "current": "AC", "power_w": 22080, "proxy": "ams-depot", "pv_w": 150000, "demand": "depot"}


def test_demand_profiles_follow_the_local_day():
    assert demand_share("office", 3) == 0.0
    assert demand_share("office", 8) == 0.8
    assert demand_share("depot", 23) == 0.9
    assert demand_share("unknown", 12) == 1.0


def test_active_stations_is_stable_and_rounds_up():
    ids = ["S-3", "S-1", "S-2"]
    assert active_stations(ids, 0.5) == {"S-1", "S-2"}
    assert active_stations(ids, 0.0) == set()


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


def engine(sites, clients):
    return WorldEngine(sites, clients, LimitArbiter(clients.csms))


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


async def test_demand_turns_arrivals_off_outside_busy_hours(monkeypatch):
    monkeypatch.setattr("control.world.local_hour", lambda now=None: 3)  # office profile 0.0
    office = {**DEPOT, "code": "EHV-OFFICE", "demand": "office"}
    clients = Clients()
    eng = engine([office], clients)
    eng.enabled = True
    changes = await eng.tick(snap("EHV-OFFICE", 3), {})
    assert clients.simulator.calls[0] == ("atg", ("EHV-OFFICE-1", "EHV-OFFICE-2", "EHV-OFFICE-3"), False)
    assert changes == ["EHV-OFFICE: arrivals at 0/3 stations for this hour"]
    assert await eng.tick(snap("EHV-OFFICE", 3), {}) == []  # same target again: re-applied silently
    clients.simulator.calls.clear()
    await eng.disable()
    assert clients.simulator.calls[0][2] is True


def test_price_rule_only_for_depot_and_when_expensive():
    from control.world import price_rule
    assert price_rule(HUB, {"available": True}) is None
    hot = price_rule(DEPOT, {"available": True, "now_eur_mwh": 210.0, "expensive": True, "expensive_from_eur_mwh": 150})
    calm = price_rule(DEPOT, {"available": True, "now_eur_mwh": 80.0, "expensive": False, "expensive_from_eur_mwh": 150})
    assert hot.active and "top 20%" in hot.text
    assert not calm.active and not price_rule(DEPOT, None).active
