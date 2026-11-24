from control.battery import SOC_MAX, SOC_MIN, Battery, BatteryFleet, Spec, decide

SPEC = Spec(100, 50)
CHEAP = {"available": True, "rank": 0.1, "expensive": False}
PRICY = {"available": True, "rank": 0.95, "expensive": True}
MID = {"available": True, "rank": 0.5, "expensive": False}


def test_covers_load_above_the_cap_first():
    power, mode, _ = decide(Battery(SPEC, 50), power_w=130_000, cap_w=100_000, solar_w=0, prices=CHEAP)
    assert (power, mode) == (-30_000, "discharging")


def test_stores_spare_solar_then_cheap_grid_power_within_headroom():
    assert decide(Battery(SPEC, 50), 10_000, 100_000, 30_000, MID)[:2] == (20_000, "charging")
    assert decide(Battery(SPEC, 50), 80_000, 100_000, 0, CHEAP)[:2] == (20_000, "charging")  # only the headroom
    assert decide(Battery(SPEC, 50), 100_000, 100_000, 0, CHEAP)[1] == "idle"  # no headroom: never adds load


def test_releases_energy_when_expensive_and_respects_limits():
    assert decide(Battery(SPEC, 50), 40_000, 100_000, 0, PRICY)[:2] == (-40_000, "discharging")
    assert decide(Battery(SPEC, SOC_MIN * 100), 40_000, 100_000, 0, PRICY)[1] == "idle"
    assert decide(Battery(SPEC, SOC_MAX * 100), 0, 100_000, 50_000, CHEAP)[1] == "idle"


def test_fleet_integrates_soc_with_efficiency_and_can_be_switched_off():
    fleet = BatteryFleet([{"code": "S", "demand": "office"}], start_soc=0.5)  # office: 100 kWh / 50 kW
    fleet.tick([{"code": "S", "power_w": 0, "cap_w": 80_000, "solar_w": 0}], CHEAP, 3600)
    assert fleet.status()["S"]["soc_kwh"] == 50 + 50 * 0.9
    fleet.enabled = False
    fleet.tick([{"code": "S", "power_w": 0, "cap_w": 80_000, "solar_w": 0}], CHEAP, 3600)
    assert fleet.status()["S"]["mode"] == "off" and fleet.support_w("S") == 0
