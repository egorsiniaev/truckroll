"""Local time of day for the map: morning, day, evening, night, from real sunrise and sunset."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Amsterdam")
TWILIGHT = timedelta(minutes=30)
MORNING_LENGTH = timedelta(hours=3)
EVENING_LENGTH = timedelta(hours=2)


def _parse_local(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=TZ)
    except ValueError:
        return None


def daylight(now: datetime, sunrise: str | None, sunset: str | None) -> dict:
    local = now.astimezone(TZ)
    rise, set_ = _parse_local(sunrise), _parse_local(sunset)
    if rise is None or set_ is None:
        # No sun times yet: fall back to clock hours.
        h = local.hour
        phase = "night" if h < 7 or h >= 18 else "morning" if h < 11 else "evening" if h >= 16 else "day"
    elif local < rise - TWILIGHT or local >= set_ + TWILIGHT:
        phase = "night"
    elif local < rise + MORNING_LENGTH:
        phase = "morning"
    elif local >= set_ - EVENING_LENGTH:
        phase = "evening"
    else:
        phase = "day"
    return {
        "phase": phase,
        "local_time": local.strftime("%H:%M"),
        "date": local.strftime("%a %d %b"),
        "sunrise": rise.strftime("%H:%M") if rise else None,
        "sunset": set_.strftime("%H:%M") if set_ else None,
    }
