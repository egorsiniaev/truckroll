from datetime import UTC, datetime

import pytest

from control.economics import GRID_FEES_EUR_KWH, Economics, demand_factor, tariff

HUB = {"code": "A2-HUB", "demand": "highway"}
DEPOT = {"code": "AMS-DEPOT", "demand": "depot"}
SPIKE = {"available": True, "now_eur_mwh": 300.0, "avg_eur_mwh": 150.0}
CALM = {"available": True, "now_eur_mwh": 120.0, "avg_eur_mwh": 150.0}


def test_dynamic_pricing_passes_spikes_to_public_sites_only():
    assert tariff(HUB, SPIKE, dynamic=True) == pytest.approx(0.69 * 1.6)  # capped
    assert tariff(HUB, CALM, dynamic=True) == pytest.approx(0.69 * (1 - 0.2 * 0.8), abs=0.001)  # 20 % dip -> 16 % off
    assert tariff(HUB, SPIKE, dynamic=False) == 0.69


def test_fleet_and_office_contracts_follow_the_cost_of_power():
    office = {"code": "EHV-OFFICE", "demand": "office"}
    buy = 0.300 + GRID_FEES_EUR_KWH
    assert tariff(DEPOT, SPIKE, dynamic=True) == pytest.approx(buy + 0.06)
    assert tariff(DEPOT, SPIKE, dynamic=False) == pytest.approx(buy + 0.06)  # a contract, not dynamic pricing
    assert tariff(office, CALM, dynamic=True) == pytest.approx(0.120 + GRID_FEES_EUR_KWH + 0.10)
    assert tariff(DEPOT, None, dynamic=True) == pytest.approx(0.100 + GRID_FEES_EUR_KWH + 0.06)  # no prices: fallback


def test_dearer_public_charger_loses_drivers_fleet_barely():
    assert demand_factor(HUB, 0.69) == 1.0
    assert demand_factor(HUB, 0.69 * 1.6) < 0.55
    assert demand_factor(DEPOT, 0.32 * 1.6) > 0.9


def test_indexed_contract_never_loses_money_on_grid_energy():
    econ = Economics([DEPOT])
    econ.accrue([{"code": "AMS-DEPOT", "power_w": 20_000}], SPIKE, 3600, datetime(2026, 11, 24, 18, tzinfo=UTC))
    assert econ.summary()["sites"]["AMS-DEPOT"]["margin_eur"] == pytest.approx(20 * 0.06)


def test_accrue_uses_solar_first_then_grid_at_spot_plus_fees():
    econ = Economics([HUB], dynamic=False)
    now = datetime(2026, 11, 24, 12, tzinfo=UTC)
    econ.accrue([{"code": "A2-HUB", "power_w": 100_000, "solar_w": 20_000}], CALM, 3600, now)
    led = econ.summary()["sites"]["A2-HUB"]
    assert led["energy_kwh"] == 100 and led["solar_kwh"] == 20 and led["grid_kwh"] == 80
    assert led["revenue_eur"] == pytest.approx(69.0)
    assert led["cost_eur"] == pytest.approx(80 * (0.12 + GRID_FEES_EUR_KWH))
    assert led["margin_eur_h"] == pytest.approx(69.0 - 80 * (0.12 + GRID_FEES_EUR_KWH), abs=0.01)


def test_ledger_rolls_over_at_local_midnight_and_counts_lost_revenue():
    econ = Economics([HUB])
    econ.accrue([{"code": "A2-HUB", "power_w": 50_000}], None, 3600, datetime(2026, 11, 24, 22, tzinfo=UTC))
    econ.lost("A2-HUB", 10, None)
    assert econ.summary()["fleet"]["lost_eur"] == pytest.approx(6.9)
    econ.accrue([{"code": "A2-HUB", "power_w": 0}], None, 1, datetime(2026, 11, 24, 23, 30, tzinfo=UTC))  # 00:30 local
    assert econ.summary()["fleet"]["energy_kwh"] == 0


def test_cheap_hours_and_solar_give_discounts_that_draw_drivers():
    cheap = {"available": True, "now_eur_mwh": 50.0, "avg_eur_mwh": 150.0}
    t = tariff(HUB, cheap, dynamic=True)
    assert t == pytest.approx(0.69 * 0.6)  # floor
    assert demand_factor(HUB, t) == 1.8  # capped
    assert tariff(HUB, None, dynamic=True, solar_share=0.6) == pytest.approx(0.69 * 0.9)
    assert tariff(DEPOT, cheap, dynamic=True) == pytest.approx(0.05 + GRID_FEES_EUR_KWH + 0.06)  # follows cost down


def test_battery_charging_adds_and_discharging_removes_grid_energy():
    econ = Economics([HUB], dynamic=False)
    now = datetime(2026, 11, 24, 12, tzinfo=UTC)
    econ.accrue([{"code": "A2-HUB", "power_w": 100_000, "battery": {"power_w": -40_000}}], CALM, 3600, now)
    assert econ.summary()["sites"]["A2-HUB"]["grid_kwh"] == 60
    econ.accrue([{"code": "A2-HUB", "power_w": 0, "battery": {"power_w": 50_000}}], CALM, 3600, now)
    assert econ.summary()["sites"]["A2-HUB"]["grid_kwh"] == 110
