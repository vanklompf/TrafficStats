"""
Intrusion event media matching and video conversion.

Scans the camera FTP upload directory for JPG snapshots and DAV recordings,
matches them to intrusion events by timestamp proximity, and provides
ffmpeg-based DAV-to-MP4 conversion with an LRU disk cache.
"""

import logging
import os
import re
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

MEDIA_PATH = os.environ.get("INTRUSION_MEDIA_PATH", "/media")
VIDEO_CACHE_DIR = os.environ.get("VIDEO_CACHE_DIR", "/data/video_cache")
VIDEO_CACHE_MAX_BYTES = (
    float(os.environ.get("VIDEO_CACHE_MAX_GB", "20")) * 1024 * 1024 * 1024
)

# Max timestamp distance (seconds) to consider a file a match for an event
MATCH_THRESHOLD_SECS = 30

# ---------------------------------------------------------------------------
# Camera timezone handling
# ---------------------------------------------------------------------------
# Camera FTP uploads use the camera's local time in filenames.  Events in the
# database are stored in UTC.  We need the camera timezone to convert file
# timestamps to UTC before matching.
#
# The system timezone (set via the standard TZ env var in Docker) is used.
# ZoneInfo gives us correct DST handling.

_local_tz: ZoneInfo | None = None


def _get_local_tz() -> ZoneInfo:
    """Return the system local timezone (from TZ env var).  Cached."""
    global _local_tz
    if _local_tz is not None:
        return _local_tz

    tz_name = os.environ.get("TZ", "").strip()
    if tz_name:
        try:
            _local_tz = ZoneInfo(tz_name)
            logger.info("Local timezone from TZ: %s", tz_name)
            return _local_tz
        except Exception:
            logger.warning("Invalid TZ=%s, falling back to UTC", tz_name)

    _local_tz = ZoneInfo("UTC")
    logger.info("Local timezone: UTC (TZ not set)")
    return _local_tz


def _camera_to_utc(naive_dt: datetime) -> datetime:
    """Convert a naive datetime in local (camera) time to naive UTC."""
    tz = _get_local_tz()
    aware = naive_dt.replace(tzinfo=tz)
    utc_aware = aware.astimezone(timezone.utc)
    return utc_aware.replace(tzinfo=None)

# Regex for JPG filenames: 001_YYYYMMDDHHmmss_[TYPE][0@0][0].jpg
_JPG_RE = re.compile(r"^\d+_(\d{14})_\[.*\].*\.jpg$", re.IGNORECASE)

# Regex for DAV filenames: HH.MM.SS-HH.MM.SS[TYPE][0@0][0].dav
_DAV_RE = re.compile(
    r"^(\d{2})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2})\[.*\].*\.dav$",
    re.IGNORECASE,
)

_cache_lock = threading.Lock()


def _parse_jpg_timestamp(filename: str) -> datetime | None:
    """Extract naive datetime from a JPG filename (camera-local time)."""
    m = _JPG_RE.match(filename)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _parse_dav_time_range(
    filename: str, date_str: str
) -> tuple[datetime, datetime] | None:
    """Extract (start, end) datetimes from a DAV filename + parent date dir."""
    m = _DAV_RE.match(filename)
    if not m:
        return None
    try:
        base = datetime.strptime(date_str, "%Y-%m-%d")
        start = base.replace(
            hour=int(m.group(1)), minute=int(m.group(2)), second=int(m.group(3))
        )
        end = base.replace(
            hour=int(m.group(4)), minute=int(m.group(5)), second=int(m.group(6))
        )
        if end < start:
            end += timedelta(days=1)
        return start, end
    except (ValueError, TypeError):
        return None


def _list_date_dir(date_str: str) -> Path | None:
    """Return the Path for a date directory if it exists."""
    p = Path(MEDIA_PATH) / date_str
    return p if p.is_dir() else None


def match_media_for_events(
    events: list[dict], date_str: str
) -> list[dict]:
    """
    For each event dict (with 'id' and 'timestamp'), find the best matching
    JPG snapshot and DAV recording from the filesystem.

    Camera FTP filenames use the camera's local timezone while event
    timestamps are UTC.  We convert file timestamps to UTC before comparing,
    and also scan adjacent date directories to handle the date boundary shift
    that occurs when the camera timezone differs from UTC.

    Returns a new list of dicts with added keys:
        snapshot / video       – filename (or None)
        snapshot_date / video_date – date directory the file lives in
    """
    base_date = datetime.strptime(date_str, "%Y-%m-%d").date()

    # Scan event-date and neighbouring directories (timezone offset can
    # push camera files into the previous or next calendar day).
    jpgs: list[tuple[str, datetime, str]] = []        # (fname, utc_ts, date_dir)
    davs: list[tuple[str, datetime, datetime, str]] = []  # (fname, utc_start, utc_end, date_dir)

    for delta in (-1, 0, 1):
        d = base_date + timedelta(days=delta)
        ds = d.strftime("%Y-%m-%d")
        date_dir = _list_date_dir(ds)
        if date_dir is None:
            continue
        try:
            files = os.listdir(date_dir)
        except OSError:
            continue
        for f in files:
            ts = _parse_jpg_timestamp(f)
            if ts is not None:
                jpgs.append((f, _camera_to_utc(ts), ds))
                continue
            rng = _parse_dav_time_range(f, ds)
            if rng is not None:
                davs.append((f, _camera_to_utc(rng[0]), _camera_to_utc(rng[1]), ds))

    jpgs.sort(key=lambda x: x[1])
    davs.sort(key=lambda x: x[1])

    results = []
    for ev in events:
        ev_ts = datetime.strptime(ev["timestamp"], "%Y-%m-%d %H:%M:%S")

        # Find closest JPG within threshold
        best_jpg = None
        best_jpg_date = None
        best_jpg_dist = MATCH_THRESHOLD_SECS + 1
        for fname, ts, ds in jpgs:
            dist = abs((ts - ev_ts).total_seconds())
            if dist < best_jpg_dist:
                best_jpg_dist = dist
                best_jpg = fname
                best_jpg_date = ds

        # Find DAV whose range contains the event, or closest by midpoint
        best_dav = None
        best_dav_date = None
        best_dav_dist = MATCH_THRESHOLD_SECS + 1
        for fname, start, end, ds in davs:
            if start <= ev_ts <= end:
                best_dav = fname
                best_dav_date = ds
                best_dav_dist = 0
                break
            mid = start + (end - start) / 2
            dist = abs((mid - ev_ts).total_seconds())
            if dist < best_dav_dist:
                best_dav_dist = dist
                best_dav = fname
                best_dav_date = ds

        results.append({
            **ev,
            "snapshot": best_jpg if best_jpg_dist <= MATCH_THRESHOLD_SECS else None,
            "snapshot_date": best_jpg_date if best_jpg_dist <= MATCH_THRESHOLD_SECS else None,
            "video": best_dav if best_dav_dist <= MATCH_THRESHOLD_SECS else None,
            "video_date": best_dav_date if best_dav_dist <= MATCH_THRESHOLD_SECS else None,
        })

    return results


# ---------------------------------------------------------------------------
# Video conversion with LRU cache
# ---------------------------------------------------------------------------


def _ensure_cache_dir(date_str: str) -> Path:
    p = Path(VIDEO_CACHE_DIR) / date_str
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_cached_video_path(date_str: str, dav_filename: str) -> Path | None:
    """Return the cached MP4 path if it exists, else None."""
    mp4_name = Path(dav_filename).stem + ".mp4"
    cached = Path(VIDEO_CACHE_DIR) / date_str / mp4_name
    if cached.is_file():
        cached.touch()  # update mtime for LRU
        return cached
    return None


def convert_dav_to_mp4(date_str: str, dav_filename: str) -> Path | None:
    """
    Convert a DAV file to a browser-friendly MP4 using ffmpeg.

    Returns the path to the cached MP4, or None on failure.
    """
    source = Path(MEDIA_PATH) / date_str / dav_filename
    if not source.is_file():
        logger.warning("DAV source not found: %s", source)
        return None

    cached = get_cached_video_path(date_str, dav_filename)
    if cached is not None:
        return cached

    cache_dir = _ensure_cache_dir(date_str)
    mp4_name = Path(dav_filename).stem + ".mp4"
    output = cache_dir / mp4_name
    tmp_output = output.with_suffix(".tmp.mp4")

    cmd = [
        "ffmpeg", "-y", "-i", str(source),
        "-c:v", "libx264", "-profile:v", "baseline", "-level", "3.1",
        "-pix_fmt", "yuv420p",
        "-b:v", "1500k", "-maxrate", "2000k", "-bufsize", "3000k",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        "-movflags", "+faststart",
        str(tmp_output),
    ]

    try:
        logger.info("Converting %s -> %s", source, output)
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            timeout=120,
        )
        tmp_output.rename(output)
        logger.info("Conversion complete: %s (%.1f KB)", output, output.stat().st_size / 1024)
    except subprocess.CalledProcessError as e:
        logger.error("ffmpeg failed for %s: %s", source, e.stderr[-500:] if e.stderr else "")
        tmp_output.unlink(missing_ok=True)
        return None
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg timed out for %s", source)
        tmp_output.unlink(missing_ok=True)
        return None

    _enforce_cache_limit()
    return output


def _enforce_cache_limit():
    """Delete oldest cached files until total size is under the limit."""
    with _cache_lock:
        cache_root = Path(VIDEO_CACHE_DIR)
        if not cache_root.is_dir():
            return

        all_files = sorted(cache_root.rglob("*.mp4"), key=lambda p: p.stat().st_mtime)
        total = sum(f.stat().st_size for f in all_files)

        while total > VIDEO_CACHE_MAX_BYTES and all_files:
            oldest = all_files.pop(0)
            size = oldest.stat().st_size
            try:
                oldest.unlink()
                total -= size
                logger.info("Cache evict: %s (%.1f MB freed)", oldest, size / 1024 / 1024)
            except OSError:
                pass
