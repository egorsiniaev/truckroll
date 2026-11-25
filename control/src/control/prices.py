"""Day-ahead electricity prices for the Netherlands from the ENTSO-E Transparency Platform.

The token comes from ENTSOE_API_TOKEN at runtime. It is part of the request URL, so never log URLs or exceptions
from this module verbatim.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

URL = "https://web-api.tp.entsoe.eu/api"
NL_ZONE = "10YNL----------L"
TZ = ZoneInfo("Europe/Amsterdam")
RESOLUTIONS = {"PT15M": timedelta(minutes=15), "PT30M": timedelta(minutes=30), "PT60M": timedelta(hours=1)}
EXPENSIVE_PERCENTILE = 0.8


class PriceError(Exception):
    """Prices could not be fetched or parsed. The message never contains the token."""


@dataclass(frozen=True)
class Slot:
    start: datetime
    end: datetime
    eur_mwh: float


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(node: ET.Element, name: str) -> ET.Element | None:
    return next((c for c in node.iter() if _local(c.tag) == name), None)


def parse_day_ahead(xml_text: str) -> list[Slot]:
    """Parse an A44 Publication_MarketDocument. Missing positions repeat the previous price (curve type A03)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise PriceError(f"unparseable response: {exc}") from None
    if _local(root.tag) == "Acknowledgement_MarketDocument":
        reason = _find(root, "text")
        raise PriceError(f"ENTSO-E: {reason.text if reason is not None else 'no data'}")
    slots: dict[datetime, Slot] = {}
    for period in (n for n in root.iter() if _local(n.tag) == "Period"):
        start_el, res_el = _find(period, "start"), _find(period, "resolution")
        if start_el is None or res_el is None or res_el.text not in RESOLUTIONS:
            continue
        start = datetime.strptime(start_el.text, "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC)
        step = RESOLUTIONS[res_el.text]
        end_el = _find(period, "end")
        end = datetime.strptime(end_el.text, "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC) if end_el is not None else None
        points = {}
        for point in (n for n in period if _local(n.tag) == "Point"):
            pos, price = _find(point, "position"), _find(point, "price.amount")
            if pos is not None and price is not None:
                points[int(pos.text)] = float(price.text)
        if not points:
            continue
        last_pos = int((end - start) / step) if end else max(points)
        current = None
        for pos in range(1, last_pos + 1):
            current = points.get(pos, current)
            if current is None:
                continue
            slot_start = start + (pos - 1) * step
            slots[slot_start] = Slot(slot_start, slot_start + step, current)
    if not slots:
        raise PriceError("no price points in response")
    return sorted(slots.values(), key=lambda s: s.start)


def summarize(slots: list[Slot], now: datetime) -> dict:
    """Current price and where it sits in the local day."""
    today = now.astimezone(TZ).date()
    day = [s for s in slots if s.start.astimezone(TZ).date() == today]
    current = next((s for s in slots if s.start <= now < s.end), None)
    if not day or current is None:
        return {"available": False}
    values = sorted(s.eur_mwh for s in day)
    rank = sum(v <= current.eur_mwh for v in values) / len(values)
    threshold = values[min(len(values) - 1, int(EXPENSIVE_PERCENTILE * len(values)))]
    return {
        "available": True,
        "now_eur_mwh": round(current.eur_mwh, 2),
        "min_eur_mwh": round(values[0], 2),
        "max_eur_mwh": round(values[-1], 2),
        "avg_eur_mwh": round(sum(values) / len(values), 2),
        "rank": round(rank, 3),
        "expensive": current.eur_mwh >= threshold,
        "expensive_from_eur_mwh": round(threshold, 2),
        "curve": [{"t": s.start.astimezone(TZ).strftime("%H:%M"), "p": round(s.eur_mwh, 1)} for s in day],
    }


class PriceClient:
    def __init__(self, http: httpx.AsyncClient, token: str) -> None:
        self._http = http
        self._token = token

    async def day_ahead(self, now: datetime) -> list[Slot]:
        local_midnight = now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        start = local_midnight.astimezone(UTC)
        end = (local_midnight + timedelta(days=1)).astimezone(UTC)
        params = {
            "securityToken": self._token,
            "documentType": "A44",
            "in_Domain": NL_ZONE,
            "out_Domain": NL_ZONE,
            "periodStart": start.strftime("%Y%m%d%H%M"),
            "periodEnd": end.strftime("%Y%m%d%H%M"),
        }
        try:
            res = await self._http.get(URL, params=params, timeout=60.0)  # ENTSO-E takes ~16 s from Hetzner; runs in its own loop
        except httpx.HTTPError as exc:
            raise PriceError(f"request failed: {exc.__class__.__name__}") from None
        if res.status_code == 401:
            raise PriceError("token rejected (HTTP 401)")
        if res.status_code >= 400 and "Acknowledgement" not in res.text:
            raise PriceError(f"HTTP {res.status_code}")
        return parse_day_ahead(res.text)
