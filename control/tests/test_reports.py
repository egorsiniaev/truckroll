from control.reports import applicability


def inc(kind, ttr):
    return {"id": "I", "kind": kind, "opened": "2026-11-25T10:00:00Z", "ttr_s": ttr, "age_s": ttr}


def test_long_site_outage_is_significant_under_nis2_with_deadlines():
    a = applicability(inc("site_outage", 2400))
    assert a["nis2"]["applies"] and a["nis2"]["deadlines"]["early_warning"].startswith("2026-11-26T10:00")
    assert a["nis2"]["deadlines"]["notification"].startswith("2026-11-28T10:00")
    assert a["cra"]["applies"] is False


def test_short_or_local_problems_are_not_reportable():
    for kind, ttr in (("site_outage", 300), ("fault", 99999), ("auth_slow", 120)):
        assert applicability(inc(kind, ttr))["nis2"]["applies"] is False


def test_fleet_wide_backend_failure_is_significant():
    assert applicability(inc("auth_slow", 900))["nis2"]["applies"]
