import json

import httpx
import pytest

from control.clients import ToxiproxyClient, UpstreamError, WeatherClient


def transport(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_latency_replaces_existing_toxic():
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        if req.method == "GET":
            return httpx.Response(200, json=[{"name": "latency"}])
        return httpx.Response(200, json={})

    await ToxiproxyClient(transport(handler), "http://tp").set_latency("a2-hub", 500)
    assert [s[:2] for s in seen] == [("GET", "/proxies/a2-hub/toxics"), ("DELETE", "/proxies/a2-hub/toxics/latency"),
                                     ("POST", "/proxies/a2-hub/toxics")]
    assert seen[2][2]["attributes"]["latency"] == 500


async def test_zero_latency_only_removes():
    seen = []

    def handler(req):
        seen.append(req.method)
        return httpx.Response(200, json=[] if req.method == "GET" else {})

    await ToxiproxyClient(transport(handler), "http://tp").set_latency("a2-hub", 0)
    assert seen == ["GET"]


async def test_http_error_becomes_upstream_error():
    client = ToxiproxyClient(transport(lambda req: httpx.Response(500, text="boom")), "http://tp")
    with pytest.raises(UpstreamError, match="HTTP 500"):
        await client.set_enabled("x", False)


async def test_weather_parses_multiple_locations():
    rows = [{"current": {"temperature_2m": 5.1, "wind_gusts_10m": 40, "cloud_cover": 90, "shortwave_radiation": 20,
                         "is_day": 1, "weather_code": 3, "time": "t"}},
            {"current": {"temperature_2m": 6.0, "wind_gusts_10m": 10, "cloud_cover": 10, "shortwave_radiation": 300,
                         "is_day": 0, "weather_code": 0, "time": "t"}}]
    out = await WeatherClient(transport(lambda req: httpx.Response(200, json=rows))).current([(1, 2), (3, 4)])
    assert out[0]["temp_c"] == 5.1 and out[0]["is_day"] is True
    assert out[1]["radiation_wm2"] == 300 and out[1]["is_day"] is False
