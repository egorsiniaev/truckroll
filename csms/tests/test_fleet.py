from csms.fleet import FleetStore, site_of


def test_site_is_station_id_without_trailing_number():
    assert site_of("AMS-DEPOT-00007") == "AMS-DEPOT"
    assert site_of("DESK-01") == "DESK"
    assert site_of("lonely") == "lonely"


def test_status_update_returns_new_snapshot_and_leaves_old_one_untouched():
    fleet = FleetStore()
    fleet.connect("AMS-DEPOT-00001", now=1.0)
    before = fleet.get("AMS-DEPOT-00001")

    fleet.set_status("AMS-DEPOT-00001", connector_id=1, status="Charging", error_code="NoError", now=2.0)

    after = fleet.get("AMS-DEPOT-00001")
    assert after.connectors[1].status == "Charging"
    assert 1 not in before.connectors
    assert after.last_seen == 2.0


def test_transaction_lifecycle_tracks_connector():
    fleet = FleetStore()
    fleet.connect("HWY-A2-00001", now=0.0)

    tx_id = fleet.start_transaction("HWY-A2-00001", connector_id=2, now=1.0)
    assert fleet.get("HWY-A2-00001").connectors[2].transaction_id == tx_id

    station_id = fleet.stop_transaction(tx_id, now=2.0)
    assert station_id == "HWY-A2-00001"
    assert fleet.get("HWY-A2-00001").connectors[2].transaction_id is None
    assert fleet.get("HWY-A2-00001").connectors[2].power_w == 0.0


def test_stop_of_unknown_transaction_returns_none():
    assert FleetStore().stop_transaction(999, now=1.0) is None


def test_stale_disconnect_does_not_mark_reconnected_station_offline():
    fleet = FleetStore()
    old = fleet.connect("UTR-RETAIL-00001", now=1.0)
    new = fleet.connect("UTR-RETAIL-00001", now=2.0)

    fleet.disconnect("UTR-RETAIL-00001", token=old)
    assert fleet.get("UTR-RETAIL-00001").connected is True

    fleet.disconnect("UTR-RETAIL-00001", token=new)
    assert fleet.get("UTR-RETAIL-00001").connected is False


def test_meter_values_update_power_and_energy():
    fleet = FleetStore()
    fleet.connect("DESK-01", now=0.0)
    fleet.record_meter("DESK-01", connector_id=1, power_w=7400.0, energy_wh=1234.0, now=5.0)

    connector = fleet.get("DESK-01").connectors[1]
    assert connector.power_w == 7400.0
    assert connector.energy_wh == 1234.0


def test_meter_values_keep_previous_reading_when_one_is_missing():
    fleet = FleetStore()
    fleet.connect("DESK-01", now=0.0)
    fleet.record_meter("DESK-01", connector_id=1, power_w=100.0, energy_wh=10.0, now=1.0)
    fleet.record_meter("DESK-01", connector_id=1, power_w=None, energy_wh=20.0, now=2.0)

    connector = fleet.get("DESK-01").connectors[1]
    assert connector.power_w == 100.0
    assert connector.energy_wh == 20.0


def test_close_stale_after_reboot_and_on_available():
    fleet = FleetStore()
    fleet.connect("S-1", now=0.0)
    a = fleet.start_transaction("S-1", connector_id=1, now=1.0)
    b = fleet.start_transaction("S-1", connector_id=2, now=1.0)

    assert fleet.close_stale("S-1", connector_id=2) == [b]
    assert fleet.get("S-1").connectors[2].transaction_id is None
    assert fleet.get("S-1").connectors[1].transaction_id == a

    assert fleet.close_stale("S-1") == [a]
    assert fleet.stop_transaction(a, now=2.0) is None
    assert fleet.close_stale("S-1") == []


def test_adopt_transaction_from_before_a_restart_and_ids_do_not_collide():
    fleet = FleetStore()
    fleet.connect("S-1", now=0.0)
    assert fleet.adopt_transaction("S-1", connector_id=1, tx_id=461, now=1.0) is True
    assert fleet.adopt_transaction("S-1", connector_id=1, tx_id=461, now=1.0) is False
    assert fleet.get("S-1").connectors[1].transaction_id == 461
    assert fleet.stop_transaction(461, now=2.0) == "S-1"
    assert fleet.start_transaction("S-1", connector_id=1, now=3.0) > 1_000_000_000


def test_status_since_only_moves_when_the_status_changes():
    fleet = FleetStore()
    fleet.set_status("S-1", 1, "Preparing", "NoError", now=10.0)
    fleet.set_status("S-1", 1, "Preparing", "NoError", now=50.0)
    assert fleet.get("S-1").connectors[1].status_since == 10.0
    fleet.set_status("S-1", 1, "Charging", "NoError", now=60.0)
    assert fleet.get("S-1").connectors[1].status_since == 60.0


def test_new_session_starts_from_its_own_meter_start_not_the_previous_reading():
    fleet = FleetStore()
    fleet.connect("A2-HUB-00001", now=0.0)
    old = fleet.start_transaction("A2-HUB-00001", connector_id=1, now=1.0, meter_start_wh=0.0)
    fleet.record_meter("A2-HUB-00001", 1, power_w=50_000.0, energy_wh=30_000.0, now=2.0)
    fleet.stop_transaction(old, now=3.0)

    # The register restarts per session; until the first MeterValues, energy must read the new start, not 30 kWh.
    fleet.start_transaction("A2-HUB-00001", connector_id=1, now=4.0, meter_start_wh=0.0)

    conn = fleet.get("A2-HUB-00001").connectors[1]
    assert conn.energy_wh - conn.meter_start_wh == 0.0
