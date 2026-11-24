import json

import pytest

from csms.auth import load_tag_list
from csms.charge_point import parse_meter_values


def test_tag_file_is_loaded(tmp_path):
    path = tmp_path / "tags.json"
    path.write_text(json.dumps(["A", "B"]))
    tags = load_tag_list(str(path), accept_any=False)
    assert tags.is_valid("A") and not tags.is_valid("C")


def test_no_tag_file_accepts_nothing_unless_accept_any():
    assert not load_tag_list(None, accept_any=False).is_valid("A")
    assert load_tag_list(None, accept_any=True).is_valid("A")


@pytest.mark.parametrize("content", ["not json", json.dumps({"a": 1}), json.dumps([1, 2])])
def test_bad_tag_file_stops_startup(tmp_path, content):
    path = tmp_path / "tags.json"
    path.write_text(content)
    with pytest.raises(SystemExit):
        load_tag_list(str(path), accept_any=False)


def test_missing_tag_file_stops_startup(tmp_path):
    with pytest.raises(SystemExit):
        load_tag_list(str(tmp_path / "nope.json"), accept_any=False)


def test_meter_values_default_measurand_is_energy_and_bad_values_are_skipped():
    power, energy = parse_meter_values(
        [{"timestamp": "t", "sampled_value": [{"value": "1.5", "unit": "kWh"}, {"value": "oops", "measurand": "Power.Active.Import"}]}]
    )
    assert power is None
    assert energy == 1500.0
