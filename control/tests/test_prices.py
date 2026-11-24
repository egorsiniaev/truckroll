from datetime import UTC, datetime

import pytest

from control.prices import PriceError, parse_day_ahead, summarize

DOC = """<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">
  <TimeSeries><Period>
    <timeInterval><start>2026-11-23T23:00Z</start><end>2026-11-24T01:00Z</end></timeInterval>
    <resolution>PT60M</resolution>
    <Point><position>1</position><price.amount>50.0</price.amount></Point>
    <Point><position>2</position><price.amount>120.5</price.amount></Point>
  </Period></TimeSeries>
  <TimeSeries><Period>
    <timeInterval><start>2026-11-24T01:00Z</start><end>2026-11-24T02:00Z</end></timeInterval>
    <resolution>PT15M</resolution>
    <Point><position>1</position><price.amount>80</price.amount></Point>
    <Point><position>3</position><price.amount>90</price.amount></Point>
  </Period></TimeSeries>
</Publication_MarketDocument>"""

ACK = """<Acknowledgement_MarketDocument xmlns="urn:x"><Reason><code>999</code><text>No matching data found</text></Reason></Acknowledgement_MarketDocument>"""


def test_parses_mixed_resolutions_and_fills_missing_positions():
    slots = parse_day_ahead(DOC)
    assert [s.eur_mwh for s in slots] == [50.0, 120.5, 80.0, 80.0, 90.0, 90.0]
    assert slots[2].start == datetime(2026, 11, 24, 1, 0, tzinfo=UTC)
    assert (slots[3].end - slots[3].start).seconds == 900


def test_acknowledgement_and_garbage_raise_without_leaking_anything():
    with pytest.raises(PriceError, match="No matching data"):
        parse_day_ahead(ACK)
    with pytest.raises(PriceError):
        parse_day_ahead("<<not xml")


def test_summary_marks_expensive_slot():
    slots = parse_day_ahead(DOC)
    summary = summarize(slots, datetime(2026, 11, 24, 0, 30, tzinfo=UTC))  # 01:30 local, the 120.5 slot
    assert summary["available"] and summary["now_eur_mwh"] == 120.5
    assert summary["expensive"] is True and summary["max_eur_mwh"] == 120.5
    assert summary["curve"][0] == {"t": "00:00", "p": 50.0}


def test_summary_without_current_slot_is_unavailable():
    assert summarize(parse_day_ahead(DOC), datetime(2026, 11, 30, tzinfo=UTC)) == {"available": False}
