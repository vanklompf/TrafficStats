"""
Intrusion event media matching and video conversion.

Scans the camera FTP upload directory for JPG snapshots and DAV recordings,
matches them to intrusion events by timestamp proximity, and provides
ffmpeg-based DAV-to-MP4 conversion with an LRU disk cache.  Also generates
and caches downscaled snapshot thumbnails for faster grid loading.
"""

import logging
import os
import re
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image

logger = logging.getLogger(__name__)

MEDIA_PATH = os.environ.get("INTRUSION_MEDIA_PATH", "/media")
VIDEO_CACHE_DIR = os.environ.get("VIDEO_CACHE_DIR", "/data/video_cache")
THUMB_CACHE_DIR = os.environ.get("THUMB_CACHE_DIR", "/data/thumb_cache")
def _parse_float_env(name: str, default: float) -> float:
    """Parse a float environment variable, falling back to *default*."""
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid number for %s=%r, using default %s", name, raw, default)
        return default


VIDEO_CACHE_MAX_BYTES = int(
    _parse_float_env("VIDEO_CACHE_MAX_GB", 20.0) * 1024 * 1024 * 1024
)

# Target video height for re-encoding (0 = no scaling).  Width is computed
# automatically to preserve the aspect ratio (-2 keeps it divisible by 2).
VIDEO_SCALE_HEIGHT = int(os.environ.get("VIDEO_SCALE_HEIGHT", "720"))

# Maximum time (seconds) for a single ffmpeg conversion.  4K HEVC → 720p
# H.264 software transcode runs at ~0.23× real-time, so a 2-minute clip
# needs ~520 s.  With QSV hardware encoding this drops to ~2-5× real-time,
# but we keep the generous default for the software fallback path.
VIDEO_FFMPEG_TIMEOUT = int(os.environ.get("VIDEO_FFMPEG_TIMEOUT", "600"))


# ---------------------------------------------------------------------------
# Hardware-accelerated encoding (Intel QSV)
# ---------------------------------------------------------------------------

def _detect_hw_accel() -> str | None:
    """Probe for Intel QSV support by encoding a tiny synthetic frame."""
    setting = os.environ.get("VIDEO_HW_ACCEL", "auto").lower().strip()
    if setting == "off":
        logger.info("Hardware encoding disabled by VIDEO_HW_ACCEL=off")
        return None

    try:
        subprocess.run(
            [
                "ffmpeg", "-v", "error",
                "-init_hw_device", "qsv=hw",
                "-f", "lavfi", "-i", "color=black:s=64x64:d=0.1",
                "-c:v", "h264_qsv", "-f", "null", "-",
            ],
            check=True,
            capture_output=True,
            timeout=10,
        )
        logger.info("Intel QSV hardware encoding is available")
        return "qsv"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        logger.info("Intel QSV not available, using software encoding")
        return None


_hw_accel: str | None = _detect_hw_accel()

# Max timestamp distance (seconds) to consider a file a match for an event
MATCH_THRESHOLD_SECS = 30

# Thumbnail settings: max width in pixels and JPEG quality (1-95)
THUMB_MAX_WIDTH = int(os.environ.get("THUMB_MAX_WIDTH", "480"))
THUMB_QUALITY = int(os.environ.get("THUMB_QUALITY", "80"))

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
_conversion_locks: dict[str, threading.Lock] = {}
_conversion_locks_guard = threading.Lock()


def _get_conversion_lock(key: str) -> threading.Lock:
    """Return a per-file lock for the given conversion key (created on first use)."""
    with _conversion_locks_guard:
        if key not in _conversion_locks:
            _conversion_locks[key] = threading.Lock()
        return _conversion_locks[key]


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

        # Find DAV whose range contains the event (with a small tolerance
        # to account for minor clock drift between event and file timestamps).
        best_dav = None
        best_dav_date = None
        tolerance = timedelta(seconds=MATCH_THRESHOLD_SECS)
        for fname, start, end, ds in davs:
            if (start - tolerance) <= ev_ts <= (end + tolerance):
                best_dav = fname
                best_dav_date = ds
                break

        results.append({
            **ev,
            "snapshot": best_jpg if best_jpg_dist <= MATCH_THRESHOLD_SECS else None,
            "snapshot_date": best_jpg_date if best_jpg_dist <= MATCH_THRESHOLD_SECS else None,
            "video": best_dav,
            "video_date": best_dav_date,
        })

    return results


# ---------------------------------------------------------------------------
# Video conversion with LRU cache
# ---------------------------------------------------------------------------


def _ensure_cache_dir(date_str: str) -> Path:
    p = Path(VIDEO_CACHE_DIR) / date_str
    p.mkdir(parents=True, exist_ok=True)
    return p


def is_video_cached(date_str: str, dav_filename: str) -> bool:
    """Check whether a cached MP4 exists (without updating mtime)."""
    mp4_name = Path(dav_filename).stem + ".mp4"
    return (Path(VIDEO_CACHE_DIR) / date_str / mp4_name).is_file()


def get_cached_video_path(date_str: str, dav_filename: str) -> Path | None:
    """Return the cached MP4 path if it exists, else None."""
    mp4_name = Path(dav_filename).stem + ".mp4"
    cached = Path(VIDEO_CACHE_DIR) / date_str / mp4_name
    if cached.is_file():
        cached.touch()  # update mtime for LRU
        return cached
    return None


def _build_ffmpeg_cmd(
    source: Path, tmp_output: Path, *, hw: str | None = None,
) -> list[str]:
    """Build the ffmpeg command list for the given acceleration mode."""
    cmd = ["ffmpeg", "-y", "-i", str(source)]

    if hw == "qsv":
        if VIDEO_SCALE_HEIGHT > 0:
            cmd += ["-vf", f"scale=-2:{VIDEO_SCALE_HEIGHT}"]
        cmd += [
            "-c:v", "h264_qsv",
            "-preset", "fast",
            "-global_quality", "23",
        ]
    else:
        if VIDEO_SCALE_HEIGHT > 0:
            cmd += ["-vf", f"scale=-2:{VIDEO_SCALE_HEIGHT}"]
        cmd += [
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
        ]

    cmd += [
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        "-movflags", "+faststart",
        str(tmp_output),
    ]
    return cmd


def convert_dav_to_mp4(date_str: str, dav_filename: str) -> Path | None:
    """
    Convert a DAV file to a browser-friendly MP4 using ffmpeg.

    Uses a per-file lock so concurrent requests for the same video
    wait for the first conversion instead of spawning duplicate ffmpeg
    processes.  When Intel QSV is available the hardware encoder is
    tried first; on failure the conversion is retried with software.

    Returns the path to the cached MP4, or None on failure.
    """
    source = Path(MEDIA_PATH) / date_str / dav_filename
    if not source.is_file():
        logger.warning("DAV source not found: %s", source)
        return None

    lock = _get_conversion_lock(f"{date_str}/{dav_filename}")
    with lock:
        cached = get_cached_video_path(date_str, dav_filename)
        if cached is not None:
            return cached

        cache_dir = _ensure_cache_dir(date_str)
        mp4_name = Path(dav_filename).stem + ".mp4"
        output = cache_dir / mp4_name
        tmp_output = output.with_suffix(".tmp.mp4")

        hw = _hw_accel
        cmd = _build_ffmpeg_cmd(source, tmp_output, hw=hw)

        try:
            label = "QSV" if hw else "software"
            logger.info("Converting %s -> %s (%s)", source, output, label)
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                timeout=VIDEO_FFMPEG_TIMEOUT,
            )
        except subprocess.CalledProcessError as e:
            tmp_output.unlink(missing_ok=True)
            if hw is not None:
                stderr_tail = e.stderr[-1000:] if e.stderr else b""
                logger.warning(
                    "QSV encode failed for %s, retrying with software: %s",
                    source, stderr_tail,
                )
                cmd = _build_ffmpeg_cmd(source, tmp_output, hw=None)
                try:
                    subprocess.run(
                        cmd,
                        check=True,
                        capture_output=True,
                        timeout=VIDEO_FFMPEG_TIMEOUT,
                    )
                except subprocess.CalledProcessError as e2:
                    stderr_tail = e2.stderr[-1000:] if e2.stderr else b""
                    logger.error(
                        "Software fallback also failed for %s (exit %s): %s",
                        source, e2.returncode, stderr_tail,
                    )
                    tmp_output.unlink(missing_ok=True)
                    return None
                except subprocess.TimeoutExpired:
                    logger.error("ffmpeg timed out for %s (timeout=%ds)", source, VIDEO_FFMPEG_TIMEOUT)
                    tmp_output.unlink(missing_ok=True)
                    return None
            else:
                stderr_tail = e.stderr[-1000:] if e.stderr else b""
                logger.error("ffmpeg failed for %s (exit %s): %s", source, e.returncode, stderr_tail)
                return None
        except subprocess.TimeoutExpired:
            logger.error("ffmpeg timed out for %s (timeout=%ds)", source, VIDEO_FFMPEG_TIMEOUT)
            tmp_output.unlink(missing_ok=True)
            return None

        tmp_output.rename(output)
        size_kb = output.stat().st_size / 1024
        logger.info("Conversion complete: %s (%.1f KB)", output, size_kb)

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


# ---------------------------------------------------------------------------
# Snapshot thumbnail cache
# ---------------------------------------------------------------------------


def _ensure_thumb_dir(date_str: str) -> Path:
    p = Path(THUMB_CACHE_DIR) / date_str
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_or_create_thumbnail(date_str: str, filename: str) -> Path | None:
    """
    Return the path to a cached thumbnail for the given snapshot.

    On first call, generates a downscaled JPEG thumbnail and caches it.
    Uses a per-file lock so concurrent requests for the same image wait
    for the first resize instead of spawning duplicate work.
    """
    thumb_dir = _ensure_thumb_dir(date_str)
    thumb_path = thumb_dir / filename

    # Fast path: already cached
    if thumb_path.is_file():
        return thumb_path

    source = Path(MEDIA_PATH) / date_str / filename
    if not source.is_file():
        return None

    lock = _get_conversion_lock(f"thumb/{date_str}/{filename}")
    with lock:
        # Re-check under lock
        if thumb_path.is_file():
            return thumb_path

        try:
            with Image.open(source) as img:
                w, h = img.size
                if w <= THUMB_MAX_WIDTH:
                    # Source is already small enough; just copy with
                    # recompression to save bytes.
                    new_w, new_h = w, h
                else:
                    ratio = THUMB_MAX_WIDTH / w
                    new_w = THUMB_MAX_WIDTH
                    new_h = int(h * ratio)

                thumb = img.resize((new_w, new_h), Image.LANCZOS)
                # Convert to RGB in case of RGBA/palette images
                if thumb.mode not in ("RGB", "L"):
                    thumb = thumb.convert("RGB")

                tmp_path = thumb_path.with_suffix(".tmp.jpg")
                thumb.save(tmp_path, "JPEG", quality=THUMB_QUALITY, optimize=True)
                tmp_path.rename(thumb_path)

            logger.info(
                "Thumbnail created: %s (%dx%d -> %dx%d, %.1f KB)",
                thumb_path, w, h, new_w, new_h,
                thumb_path.stat().st_size / 1024,
            )
            return thumb_path
        except Exception:
            logger.exception("Failed to create thumbnail for %s/%s", date_str, filename)
            thumb_path.with_suffix(".tmp.jpg").unlink(missing_ok=True)
            return None
