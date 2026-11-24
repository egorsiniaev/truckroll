import pytest

from control.clients import UpstreamError
from control.controller import RELEASE_AFTER_S, SOURCE, SiteController
from control.limits import LimitArbiter


class Csms:
    def __init__(self, offline=()):
        self.calls, self.offline = [], set(offline)

    async def set_limit(self, sid, watts):
        if sid in self.offline:
            raise UpstreamError("offline")
        self.calls.append((sid, watts))


async def test_lowest_limit_wins_and_clearing_one_source_restores_the_next():
    csms = Csms()
    arb = LimitArbiter(csms)
    await arb.set("cold", "S-1", 100000)
    await arb.set("operator", "S-1", 50000)
    await arb.set("operator", "S-1", None)
    assert csms.calls == [("S-1", 100000), ("S-1", 50000), ("S-1", 100000)]
    assert arb.sources_for("S-1") == {"cold": 100000}


async def test_offline_station_is_retried_when_back():
    csms = Csms(offline={"S-1"})
    arb = LimitArbiter(csms)
    await arb.set("operator", "S-1", 7000)
    assert csms.calls == []
    csms.offline.clear()
    await arb.retry_pending({"S-1"})
    assert csms.calls == [("S-1", 7000)]


async def test_reconcile_clears_only_unrequested_limits():
    csms = Csms()
    arb = LimitArbiter(csms)
    await arb.set("cold", "S-2", 5000)
    cleared = await arb.reconcile([{"id": "S-1", "limit_w": 9000, "connected": True},
                                   {"id": "S-2", "limit_w": 5000, "connected": True}])
    assert cleared == ["S-1"]


SITE = {"code": "A2-HUB", "cap_w": 600000}


def snap(power_w, charging=8, solar=0.0):
    stations = [{"id": f"A2-HUB-{i}", "site": "A2-HUB", "connected": True,
                 "state": "charging" if i <= charging else "available"} for i in range(1, 9)]
    return {"sites": [{"code": "A2-HUB", "power_w": power_w, "solar_w": solar}], "stations": stations}


async def test_controller_limits_over_cap_then_releases_after_quiet_period():
    t = [0.0]
    csms = Csms()
    arb = LimitArbiter(csms)
    ctl = SiteController([SITE], arb, clock=lambda: t[0])

    [ev] = await ctl.tick(snap(800000, solar=20000))
    assert ev["kind"] == "limited"
    assert ev["share_w"] == pytest.approx(620000 * 0.95 / 8)
    assert len(arb.stations_for(SOURCE)) == 8

    assert await ctl.tick(snap(560000)) == []  # limited and close to the cap: stay
    assert await ctl.tick(snap(300000)) == []  # quiet starts
    t[0] += RELEASE_AFTER_S
    [ev] = await ctl.tick(snap(300000))
    assert ev["kind"] == "released" and arb.stations_for(SOURCE) == {}


async def test_controller_ignores_sites_under_cap():
    ctl = SiteController([SITE], LimitArbiter(Csms()))
    assert await ctl.tick(snap(500000)) == []
