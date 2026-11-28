from datetime import datetime, timedelta

import pytest

from control.filings import Filings, cra_pack, due_soon, nis2_pack

SITES = {"EHV-OFFICE": {"code": "EHV-OFFICE", "stations": 10}}
OUTAGE = {"id": "INC-0007", "kind": "site_outage", "site": "EHV-OFFICE", "title": "EHV-OFFICE lost contact",
          "severity": "critical", "opened": "2026-11-28T10:48:14Z", "status": "recovered", "ttr_s": 1830,
          "metrics": {"ttd_s": 5}, "stations": [],
          "timeline": [{"time": "2026-11-28T10:48:14Z", "type": "detected", "text": "10 of 10 stations offline."},
                       {"time": "2026-11-28T10:48:15Z", "type": "response", "text": "ISP ticket opened."},
                       {"time": "2026-11-28T11:18:44Z", "type": "recovered", "text": "All back."}]}
RELEASE = {"time": "2026-11-28T17:00:02+01:00", "type": "release", "version": "1.1.2", "bump": "patch",
           "notes": "meter value rounding fix", "security": "medium", "advisory": "TRSA-2026-005", "exploited": True}


def fields(pack, stage):
    return {f["label"]: f["value"] for f in pack["stages"][stage]}


def test_nis2_pack_has_what_each_stage_requires():
    pack = nis2_pack(OUTAGE, SITES, 6.2)
    ew, note, final = fields(pack, "early_warning"), fields(pack, "notification"), fields(pack, "final_report")
    assert "Suspected unlawful or malicious act" in ew and "Possible cross-border impact" in ew
    assert "Indicators of compromise" in note and "€6" in note["Initial assessment: impact"]
    assert "Type of threat or root cause" in final and "ISP ticket" in final["Mitigation measures applied and ongoing"]
    assert pack["deadlines"]["final_report"].startswith("2027-01-01")  # a month after the 72 h notification


def test_cra_pack_covers_the_fix_rollout_and_users():
    pack = cra_pack(RELEASE, {"A": "1.1.2", "B": "1.0.9"}, {"A"})
    assert "1 of 2 chargers run 1.1.2" in fields(pack, "notification")["Corrective or mitigating measures"]
    assert "TRSA-2026-005" in fields(pack, "user_advisory")["Title"]
    assert pack["deadlines"]["final_report"].startswith("2026-12-12")  # 14 days after the fix


def test_a_stage_must_be_approved_before_it_is_marked_sent(tmp_path):
    f = Filings(tmp_path / "filings.json")
    pack = nis2_pack(OUTAGE, SITES, None)
    with pytest.raises(ValueError):
        f.mark(pack, "early_warning", "sent")
    f.mark(pack, "early_warning", "approved")
    f.mark(pack, "early_warning", "sent", reference="NCSC-123")
    again = Filings(tmp_path / "filings.json").decorate(pack)
    assert again["stages"][0]["status"] == "sent" and again["stages"][0]["reference"] == "NCSC-123"
    assert again["next"] == "notification"
    with pytest.raises(KeyError):
        f.mark(pack, "user_advisory", "approved")  # not a NIS2 stage


def test_reminder_for_the_next_stage_close_to_its_deadline(tmp_path):
    f = Filings(None)
    pack = f.decorate(nis2_pack(OUTAGE, SITES, None))
    aware = datetime.fromisoformat("2026-11-28T10:48:14+00:00")
    assert due_soon([pack], aware) == []  # 24 h left
    [due] = due_soon([pack], aware + timedelta(hours=20))
    assert due["stage"].startswith("Early warning") and due["left_h"] == 4.0
