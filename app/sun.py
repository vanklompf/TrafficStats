"""
Sunrise/sunset for collection window and chart no-collection bands.

Uses LATITUDE, LONGITUDE, TIMEZONE env vars. If unset, collection runs 24/7
and no bands are shown.
"""

import logging
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from astral import LocationInfo
from astral.sun import sun

logger = logging.getLogger(__name__)

_location: LocationInfo | None = None


def _get_location() -> LocationInfo | None:
    """Lazy-init location from env. Returns None if not configured."""
    global _location
    if _location is not None:
        return _location
    lat_s = os.environ.get("LATITUDE", "").strip()
    lon_s = os.environ.get("LONGITUDE", "").strip()
    tz_name = os.environ.get("TIMEZONE", "").strip()
    if not lat_s or not lon_s or not tz_name:
        return None
    try:
        lat = float(lat_s)
        lon = float(lon_s)
    except ValueError:
        logger.warning("Invalid LATITUDE or LONGITUDE")
        return None
    try:
        ZoneInfo(tz_name)
    except Exception:
        logger.warning("Invalid TIMEZONE: %s", tz_name)
        return None
    _location = LocationInfo("", "", tz_name, lat, lon)
    return _location


def is_daytime(utc_dt: datetime) -> bool:
    """
    True if the given UTC time is between sunrise and sunset at the configured location.
    If location is not configured, returns True (collect 24/7).
    """
    loc = _get_location()
    if loc is None:
        return True
    try:
        tz = ZoneInfo(loc.timezone)
        local_dt = utc_dt.astimezone(tz)
        s = sun(loc.observer, date=local_dt.date(), tzinfo=tz)
        return s["sunrise"] <= local_dt <= s["sunset"]
    except Exception as e:
        logger.debug("is_daytime failed: %s", e)
        return True


def get_no_collection_ranges(
    since_utc: datetime, until_utc: datetime
) -> list[dict[str, str]]:
    """
    Return list of { "start": "YYYY-MM-DD HH:MM", "end": "..." } in UTC
    for intervals when we do not collect (sunset to next sunrise).
    If location is not configured, returns [].
    """
    loc = _get_location()
    if loc is None:
        return []
    out = []
    tz = ZoneInfo(loc.timezone)
    # All unique dates that overlap [since_utc, until_utc] in local time
    since_local = since_utc.astimezone(tz)
    until_local = until_utc.astimezone(tz)
    d = since_local.date()
    end_date = until_local.date()
    while d <= end_date:
        try:
            s = sun(loc.observer, date=d, tzinfo=tz)
            sunset_local = s["sunset"]
            # Next day's sunrise
            s_next = sun(loc.observer, date=d + timedelta(days=1), tzinfo=tz)
            sunrise_next_local = s_next["sunrise"]
        except Exception as e:
            logger.debug("sun for %s failed: %s", d, e)
            d += timedelta(days=1)
            continue
        sunset_utc = sunset_local.astimezone(timezone.utc)
        sunrise_next_utc = sunrise_next_local.astimezone(timezone.utc)
        # Clip to [since_utc, until_utc]
        start = max(sunset_utc, since_utc)
        end = min(sunrise_next_utc, until_utc)
        if start < end:
            out.append({
                "start": start.strftime("%Y-%m-%d %H:%M"),
                "end": end.strftime("%Y-%m-%d %H:%M"),
            })
        d += timedelta(days=1)
    return out
