"""Thin clients for the systems the control service drives. Each raises UpstreamError with a readable message."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

import httpx
from websockets.asyncio.client import connect

log = logging.getLogger(__name__)

TIMEOUT_S = 10.0
LATENCY_TOXIC = "latency"


class UpstreamError(Exception):
    """An upstream system refused or failed a request."""


async def _json(response_coro, what: str):
    try:
        res = await response_coro
    except httpx.HTTPError as exc:
        raise UpstreamError(f"{what}: {exc.__class__.__name__}") from exc
    if res.status_code >= 400:
        detail = res.text[:200]
        raise UpstreamError(f"{what}: HTTP {res.status_code} {detail}")
    return res.json() if res.content else {}


class CsmsClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self._http, self._base = http, base_url.rstrip("/")

    async def fleet(self) -> list[dict]:
        return (await _json(self._http.get(f"{self._base}/fleet", timeout=TIMEOUT_S), "csms fleet"))["stations"]

    async def set_limit(self, station_id: str, watts: float | None) -> dict:
        url = f"{self._base}/stations/{station_id}/limit"
        return await _json(self._http.post(url, json={"watts": watts}, timeout=TIMEOUT_S + 10), "csms limit")

    async def trigger(self, station_id: str, message: str = "StatusNotification", connector_id: int | None = None) -> dict:
        url = f"{self._base}/stations/{station_id}/trigger"
        body = {"message": message, "connector_id": connector_id}
        return await _json(self._http.post(url, json=body, timeout=TIMEOUT_S + 10), "csms trigger")

    async def chaos(self, **settings) -> dict:
        return await _json(self._http.post(f"{self._base}/chaos", json=settings, timeout=TIMEOUT_S), "csms chaos")

    async def stats(self) -> dict:
        return await _json(self._http.get(f"{self._base}/stats", timeout=TIMEOUT_S), "csms stats")

    async def availability(self, station_id: str, kind: str, connector_id: int = 0) -> dict:
        url = f"{self._base}/stations/{station_id}/availability"
        body = {"type": kind, "connector_id": connector_id}
        return await _json(self._http.post(url, json=body, timeout=TIMEOUT_S + 10), "csms availability")

    async def reset(self, station_id: str, kind: str = "Soft") -> dict:
        url = f"{self._base}/stations/{station_id}/reset"
        return await _json(self._http.post(url, json={"type": kind}, timeout=TIMEOUT_S + 10), "csms reset")


class ToxiproxyClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self._http, self._base = http, base_url.rstrip("/")

    async def proxies(self) -> dict[str, dict]:
        return await _json(self._http.get(f"{self._base}/proxies", timeout=TIMEOUT_S), "toxiproxy list")

    async def set_enabled(self, proxy: str, enabled: bool) -> None:
        url = f"{self._base}/proxies/{proxy}"
        await _json(self._http.post(url, json={"enabled": enabled}, timeout=TIMEOUT_S), "toxiproxy enable")

    async def set_latency(self, proxy: str, ms: int) -> None:
        """ms=0 removes the latency toxic."""
        url = f"{self._base}/proxies/{proxy}/toxics"
        existing = await _json(self._http.get(url, timeout=TIMEOUT_S), "toxiproxy toxics")
        if any(t.get("name") == LATENCY_TOXIC for t in existing):
            await _json(self._http.delete(f"{url}/{LATENCY_TOXIC}", timeout=TIMEOUT_S), "toxiproxy remove latency")
        if ms > 0:
            toxic = {"name": LATENCY_TOXIC, "type": "latency", "stream": "downstream", "toxicity": 1.0,
                     "attributes": {"latency": ms, "jitter": ms // 5}}
            await _json(self._http.post(url, json=toxic, timeout=TIMEOUT_S), "toxiproxy add latency")


class SimulatorClient:
    """SRPC over WebSocket (protocol ui0.0.1): [uuid, procedure, payload] -> [uuid, payload]."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._hash_ids: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def _call(self, procedure: str, payload: dict) -> dict:
        request_id = str(uuid.uuid4())
        try:
            async with connect(self._url, subprotocols=["ui0.0.1"], open_timeout=TIMEOUT_S) as ws:
                await ws.send(json.dumps([request_id, procedure, payload]))
                while True:
                    raw = await asyncio.wait_for(ws.recv(), TIMEOUT_S)
                    msg = json.loads(raw)
                    if isinstance(msg, list) and msg and msg[0] == request_id:
                        return msg[1]
        except (OSError, TimeoutError, asyncio.TimeoutError) as exc:
            raise UpstreamError(f"simulator {procedure}: {exc.__class__.__name__}") from exc
        except Exception as exc:  # websockets raises its own hierarchy
            raise UpstreamError(f"simulator {procedure}: {exc}") from exc

    async def hash_id(self, station_id: str) -> str:
        async with self._lock:
            if station_id not in self._hash_ids:
                res = await self._call("listChargingStations", {})
                for cs in res.get("chargingStations", []):
                    info = cs.get("stationInfo", {})
                    if info.get("chargingStationId") and info.get("hashId"):
                        self._hash_ids[info["chargingStationId"]] = info["hashId"]
            if station_id not in self._hash_ids:
                raise UpstreamError(f"simulator does not know station {station_id}")
            return self._hash_ids[station_id]

    async def _station_call(self, station_id: str, procedure: str, extra: dict | None = None) -> dict:
        res = await self._call(procedure, {"hashIds": [await self.hash_id(station_id)], **(extra or {})})
        if res.get("status") != "success":
            raise UpstreamError(f"simulator {procedure} failed for {station_id}: {res}")
        return res

    async def set_status(self, station_id: str, connector_id: int, status: str, error_code: str) -> dict:
        return await self._station_call(
            station_id, "statusNotification", {"connectorId": connector_id, "status": status, "errorCode": error_code}
        )

    async def start_transaction(self, station_id: str, connector_id: int, id_tag: str) -> dict:
        return await self._station_call(station_id, "startTransaction", {"connectorId": connector_id, "idTag": id_tag})

    async def stop_transaction(self, station_id: str, transaction_id: int) -> dict:
        return await self._station_call(station_id, "stopTransaction", {"transactionId": transaction_id})

    async def set_running(self, station_id: str, running: bool) -> dict:
        return await self._station_call(station_id, "startChargingStation" if running else "stopChargingStation")


class WeatherClient:
    """Current conditions from Open-Meteo (free, no key) for many coordinates in one request."""

    URL = "https://api.open-meteo.com/v1/forecast"
    FIELDS = "temperature_2m,wind_gusts_10m,cloud_cover,shortwave_radiation,is_day,weather_code"

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def current(self, coords: list[tuple[float, float]]) -> list[dict]:
        params = {
            "latitude": ",".join(str(c[0]) for c in coords),
            "longitude": ",".join(str(c[1]) for c in coords),
            "current": self.FIELDS,
            "daily": "sunrise,sunset",
            "forecast_days": 1,
            "wind_speed_unit": "kmh",
            "timezone": "Europe/Amsterdam",
        }
        body = await _json(self._http.get(self.URL, params=params, timeout=TIMEOUT_S), "open-meteo")
        rows = body if isinstance(body, list) else [body]
        out = []
        for row in rows:
            cur = row.get("current", {})
            daily = row.get("daily", {})
            out.append({
                "sunrise": (daily.get("sunrise") or [None])[0],
                "sunset": (daily.get("sunset") or [None])[0],
                "temp_c": cur.get("temperature_2m"),
                "gust_kmh": cur.get("wind_gusts_10m"),
                "cloud_pct": cur.get("cloud_cover"),
                "radiation_wm2": cur.get("shortwave_radiation"),
                "is_day": bool(cur.get("is_day")),
                "code": cur.get("weather_code"),
                "time": cur.get("time"),
            })
        return out
