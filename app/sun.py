"""
Sunrise/sunset for collection window and chart no-collection bands.

Uses CITY env var. If unset, collection runs 24/7 and no bands are shown.
Latitude, longitude and timezone are looked up from the city name.
"""

import logging
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from astral import LocationInfo
from astral.sun import sun
from geopy.geocoders import Nominatim
from timezonefinder import TimezoneFinder

logger = logging.getLogger(__name__)

_location: LocationInfo | None = None
_location_failed: bool = False


def _get_location() -> LocationInfo | None:
    """Lazy-init location from CITY env. Returns None if not configured or lookup fails."""
    global _location, _location_failed
    if _location is not None:
        return _location
    if _location_failed:
        return None

    city = os.environ.get("CITY", "").strip()
    if not city:
        return None

    try:
        geolocator = Nominatim(user_agent="TrafficStats")
        result = geolocator.geocode(city)
        if result is None:
            logger.warning("CITY not found: %s", city)
            _location_failed = True
            return None
        lat, lon = result.latitude, result.longitude
        tf = TimezoneFinder()
        tz_name = tf.timezone_at(lng=lon, lat=lat)
        if not tz_name:
            logger.warning("No timezone for city: %s (lat=%.4f, lon=%.4f)", city, lat, lon)
            _location_failed = True
            return None
        _location = LocationInfo(city, "", tz_name, lat, lon)
        logger.info("Location from CITY=%s: lat=%.4f lon=%.4f tz=%s", city, lat, lon, tz_name)
        return _location
    except Exception as e:
        logger.warning("CITY lookup failed for %s: %s", city, e)
        _location_failed = True
        return None


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


def get_sun_times(for_date: date) -> dict[str, str] | None:
    """Return sunrise and sunset as UTC strings for a given local date.

    Returns ``{"sunrise": "YYYY-MM-DD HH:MM", "sunset": "YYYY-MM-DD HH:MM"}``
    or ``None`` if CITY is not configured.
    """
    loc = _get_location()
    if loc is None:
        return None
    try:
        tz = ZoneInfo(loc.timezone)
        s = sun(loc.observer, date=for_date, tzinfo=tz)
        sunrise_utc = s["sunrise"].astimezone(timezone.utc)
        sunset_utc = s["sunset"].astimezone(timezone.utc)
        return {
            "sunrise": sunrise_utc.strftime("%Y-%m-%d %H:%M"),
            "sunset": sunset_utc.strftime("%Y-%m-%d %H:%M"),
        }
    except Exception as e:
        logger.debug("get_sun_times failed for %s: %s", for_date, e)
        return None


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
