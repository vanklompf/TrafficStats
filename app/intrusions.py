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
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

MEDIA_PATH = os.environ.get("INTRUSION_MEDIA_PATH", "/media/kamera_front")
VIDEO_CACHE_DIR = os.environ.get("VIDEO_CACHE_DIR", "/data/video_cache")
VIDEO_CACHE_MAX_BYTES = (
    float(os.environ.get("VIDEO_CACHE_MAX_GB", "20")) * 1024 * 1024 * 1024
)

# Max timestamp distance (seconds) to consider a file a match for an event
MATCH_THRESHOLD_SECS = 30

# Regex for JPG filenames: 001_YYYYMMDDHHmmss_[TYPE][0@0][0].jpg
_JPG_RE = re.compile(r"^\d+_(\d{14})_\[.*\].*\.jpg$", re.IGNORECASE)

# Regex for DAV filenames: HH.MM.SS-HH.MM.SS[TYPE][0@0][0].dav
_DAV_RE = re.compile(
    r"^(\d{2})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2})\[.*\].*\.dav$",
    re.IGNORECASE,
)

_cache_lock = threading.Lock()


def _parse_jpg_timestamp(filename: str) -> datetime | None:
    """Extract UTC datetime from a JPG filename."""
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

    Returns a new list of dicts with added 'snapshot' and 'video' keys
    (filename or None).
    """
    date_dir = _list_date_dir(date_str)
    if date_dir is None:
        return [
            {**ev, "snapshot": None, "video": None}
            for ev in events
        ]

    try:
        files = os.listdir(date_dir)
    except OSError:
        files = []

    # Pre-parse all JPGs and DAVs in the directory
    jpgs: list[tuple[str, datetime]] = []
    davs: list[tuple[str, datetime, datetime]] = []

    for f in files:
        ts = _parse_jpg_timestamp(f)
        if ts is not None:
            jpgs.append((f, ts))
            continue
        rng = _parse_dav_time_range(f, date_str)
        if rng is not None:
            davs.append((f, rng[0], rng[1]))

    jpgs.sort(key=lambda x: x[1])
    davs.sort(key=lambda x: x[1])

    results = []
    for ev in events:
        ev_ts = datetime.strptime(ev["timestamp"], "%Y-%m-%d %H:%M:%S")

        # Find closest JPG within threshold
        best_jpg = None
        best_jpg_dist = MATCH_THRESHOLD_SECS + 1
        for fname, ts in jpgs:
            dist = abs((ts - ev_ts).total_seconds())
            if dist < best_jpg_dist:
                best_jpg_dist = dist
                best_jpg = fname

        # Find DAV whose range contains the event, or closest by midpoint
        best_dav = None
        best_dav_dist = MATCH_THRESHOLD_SECS + 1
        for fname, start, end in davs:
            if start <= ev_ts <= end:
                best_dav = fname
                best_dav_dist = 0
                break
            mid = start + (end - start) / 2
            dist = abs((mid - ev_ts).total_seconds())
            if dist < best_dav_dist:
                best_dav_dist = dist
                best_dav = fname

        results.append({
            **ev,
            "snapshot": best_jpg if best_jpg_dist <= MATCH_THRESHOLD_SECS else None,
            "video": best_dav if best_dav_dist <= MATCH_THRESHOLD_SECS else None,
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
