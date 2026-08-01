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
import stat
import subprocess
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image

logger = logging.getLogger(__name__)

MEDIA_PATH = os.environ.get("INTRUSION_MEDIA_PATH", "/media")
VIDEO_CACHE_DIR = os.environ.get("VIDEO_CACHE_DIR", "/data/video_cache")
THUMB_CACHE_DIR = os.environ.get("THUMB_CACHE_DIR", "/data/thumb_cache")


def get_media_path(date_str: str, filename: str | None = None) -> Path | None:
    """Return a contained, non-symlinked media directory or regular file."""
    if Path(date_str).name != date_str:
        return None
    if filename is not None and Path(filename).name != filename:
        return None

    try:
        root = Path(MEDIA_PATH).resolve(strict=True)
        date_path = Path(MEDIA_PATH) / date_str
        if date_path.is_symlink():
            return None
        candidate = date_path if filename is None else date_path / filename
        if filename is not None and candidate.is_symlink():
            return None
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None

    if resolved != root and root not in resolved.parents:
        return None
    if filename is None:
        return resolved if resolved.is_dir() else None
    return resolved if resolved.is_file() else None


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

# Camera FTP uploads use their final filename while data is still being
# written. Require an unchanged source fingerprint before opening a DAV.
MEDIA_FILE_STABLE_SECS = 10.0
MEDIA_FILE_STABLE_POLL_SECS = 2.0
MEDIA_FILE_STABLE_TIMEOUT = 30.0


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
# timestamps to UTC before matching and to display times at camera location.
#
# The TZ env var (container timezone) is used as the camera timezone.
# ZoneInfo gives us correct DST handling.

_local_tz: ZoneInfo | None = None


def _get_local_tz() -> ZoneInfo:
    """Return the camera timezone (from TZ env var).  Cached."""
    global _local_tz
    if _local_tz is not None:
        return _local_tz

    tz_name = os.environ.get("TZ", "").strip()
    if tz_name:
        try:
            _local_tz = ZoneInfo(tz_name)
            logger.info("Camera timezone from TZ: %s", tz_name)
            return _local_tz
        except Exception:
            logger.warning("Invalid TZ=%s, falling back to UTC", tz_name)

    _local_tz = ZoneInfo("UTC")
    logger.info("Camera timezone: UTC (TZ not set)")
    return _local_tz


def get_camera_timezone_name() -> str:
    """Return the IANA timezone name used for the camera (from TZ env)."""
    return _get_local_tz().key


def _camera_to_utc(naive_dt: datetime) -> datetime:
    """Convert a naive datetime in local (camera) time to naive UTC."""
    tz = _get_local_tz()
    aware = naive_dt.replace(tzinfo=tz)
    utc_aware = aware.astimezone(timezone.utc)
    return utc_aware.replace(tzinfo=None)

# Regex for JPG filenames: 001_YYYYMMDDHHmmss_[TYPE][CHANNEL@STREAM][INDEX].jpg
_JPG_RE = re.compile(
    r"^(\d+)_(\d{14})_\[([^\]]*)\]"
    r"(?:\[([^\]@]*)@([^\]]*)\])?(?:\[([^\]]*)\])?\.jpg$",
    re.IGNORECASE,
)

# Regex for DAV filenames: HH.MM.SS-HH.MM.SS[TYPE][CHANNEL@STREAM][INDEX].dav
_DAV_RE = re.compile(
    r"^(\d{2})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2})"
    r"\[([^\]]*)\](?:\[([^\]@]*)@([^\]]*)\])?(?:\[([^\]]*)\])?\.dav$",
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


def get_file_fingerprint(path: Path) -> tuple[int, int] | None:
    """Return (size, mtime_ns), or None when the source is unavailable."""
    try:
        file_stat = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(file_stat.st_mode):
        return None
    return file_stat.st_size, file_stat.st_mtime_ns


def wait_for_stable_file(
    path: Path,
    *,
    timeout: float = MEDIA_FILE_STABLE_TIMEOUT,
    stable_seconds: float = MEDIA_FILE_STABLE_SECS,
    poll_seconds: float = MEDIA_FILE_STABLE_POLL_SECS,
    stop_event: threading.Event | None = None,
) -> tuple[int, int] | None:
    """Wait until a non-empty file's size and mtime remain unchanged."""
    timeout = max(0.0, timeout)
    stable_seconds = max(0.0, stable_seconds)
    poll_seconds = max(0.01, poll_seconds)
    deadline = time.monotonic() + timeout
    previous: tuple[int, int] | None = None
    stable_since: float | None = None

    while True:
        now = time.monotonic()
        current = get_file_fingerprint(path)
        if current is not None and current[0] > 0:
            if current != previous:
                stable_since = now
            elif stable_since is not None and now - stable_since >= stable_seconds:
                return current
        else:
            stable_since = None
        previous = current

        remaining = deadline - now
        if remaining <= 0:
            return None
        wait_seconds = min(poll_seconds, remaining)
        if stop_event is not None:
            if stop_event.wait(wait_seconds):
                return None
        else:
            time.sleep(wait_seconds)


def _normalize_channel(value) -> str | None:
    if value is None or value == "":
        return None
    text = str(value)
    return str(int(text)) if text.isdigit() else text


def _parse_jpg_metadata(
    filename: str,
) -> tuple[datetime, str, str | None, str | None, str | None] | None:
    """Extract camera-local time and bracketed media identity from a JPG."""
    m = _JPG_RE.match(filename)
    if not m:
        return None
    try:
        return (
            datetime.strptime(m.group(2), "%Y%m%d%H%M%S"),
            m.group(3),
            _normalize_channel(m.group(4)),
            m.group(5),
            m.group(6),
        )
    except ValueError:
        return None


def _parse_jpg_timestamp(filename: str) -> datetime | None:
    """Extract naive datetime from a JPG filename (camera-local time)."""
    metadata = _parse_jpg_metadata(filename)
    return metadata[0] if metadata is not None else None


def _parse_dav_metadata(
    filename: str, date_str: str,
) -> tuple[datetime, datetime, str, str | None, str | None, str | None] | None:
    """Extract camera-local range and bracketed media identity from a DAV."""
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
        return (
            start, end, m.group(7), _normalize_channel(m.group(8)),
            m.group(9), m.group(10),
        )
    except (ValueError, TypeError):
        return None


def _parse_dav_time_range(
    filename: str, date_str: str
) -> tuple[datetime, datetime] | None:
    """Extract (start, end) datetimes from a DAV filename + parent date dir."""
    metadata = _parse_dav_metadata(filename, date_str)
    return metadata[:2] if metadata is not None else None


def _list_date_dir(date_str: str) -> Path | None:
    """Return the Path for a date directory if it exists."""
    return get_media_path(date_str)


def _scan_media(
    base_date: date,
) -> tuple[
    list[tuple[str, datetime, str, str, str | None, str | None, str | None]],
    list[tuple[str, datetime, datetime, str, str, str | None, str | None, str | None]],
]:
    """Return timestamped JPG and DAV candidates around a camera-local date."""
    jpgs = []
    davs = []

    for delta in (-1, 0, 1):
        d = base_date + timedelta(days=delta)
        ds = d.strftime("%Y-%m-%d")
        date_dir = _list_date_dir(ds)
        if date_dir is None:
            continue
        try:
            entries = list(os.scandir(date_dir))
        except OSError:
            continue
        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                continue
            filename = entry.name
            jpg = _parse_jpg_metadata(filename)
            if jpg is not None:
                timestamp, media_type, channel, stream, index = jpg
                jpgs.append((
                    filename, _camera_to_utc(timestamp), ds, media_type,
                    channel, stream, index,
                ))
                continue
            dav = _parse_dav_metadata(filename, ds)
            if dav is not None:
                start, end, media_type, channel, stream, index = dav
                davs.append((
                    filename,
                    _camera_to_utc(start),
                    _camera_to_utc(end),
                    ds,
                    media_type,
                    channel,
                    stream,
                    index,
                ))

    jpgs.sort(key=lambda item: (item[1], item[2], item[0]))
    davs.sort(key=lambda item: (item[1], item[2], item[3], item[0]))
    return jpgs, davs


def _select_recording(
    event_utc: datetime,
    recordings: list[tuple[str, datetime, datetime, str, str, str | None, str | None, str | None]],
    channel: str | None = None,
) -> tuple[str, datetime, datetime, str, str, str | None, str | None, str | None] | None:
    """Select the best recording deterministically, preferring containment."""
    tolerance = timedelta(seconds=MATCH_THRESHOLD_SECS)
    ranked = []

    for recording in recordings:
        filename, start, end, date_str, _, media_channel, _, _ = recording
        if channel is not None and media_channel is not None and channel != media_channel:
            continue
        if event_utc < start - tolerance or event_utc > end + tolerance:
            continue

        contained = start <= event_utc <= end
        boundary_distance = 0.0 if contained else min(
            abs((event_utc - start).total_seconds()),
            abs((event_utc - end).total_seconds()),
        )
        ranked.append((
            0 if channel is not None and channel == media_channel else 1,
            0 if contained else 1,
            boundary_distance,
            abs((event_utc - start).total_seconds()),
            (end - start).total_seconds(),
            start,
            filename,
            date_str,
            recording,
        ))

    if not ranked:
        return None
    return min(ranked, key=lambda item: item[:-1])[-1]


def get_recording_end_utc(event_utc: datetime, channel: str | None = None) -> datetime | None:
    """Return the UTC end time of the video recording that covers *event_utc*.

    Scans DAV files around the camera-local event date and uses the same
    deterministic ranking as event media enrichment. Returns ``None`` when no
    matching file is found, typically because recording is still in progress.

    *event_utc* must be a **naive** datetime representing UTC.
    """
    tz = _get_local_tz()
    event_aware = event_utc.replace(tzinfo=timezone.utc)
    event_local = event_aware.astimezone(tz)
    base_date = event_local.date()

    _, recordings = _scan_media(base_date)
    selected = _select_recording(event_utc, recordings, _normalize_channel(channel))
    return selected[2] if selected is not None else None


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
    results = []
    scans = {}
    for ev in events:
        ev_ts = datetime.strptime(
            ev.get("source_timestamp") or ev["timestamp"], "%Y-%m-%d %H:%M:%S"
        )
        event_local_date = ev_ts.replace(tzinfo=timezone.utc).astimezone(
            _get_local_tz()
        ).date()
        if event_local_date not in scans:
            scans[event_local_date] = _scan_media(event_local_date)
        jpgs, davs = scans[event_local_date]
        event_channel = _normalize_channel(ev.get("channel"))

        # Find closest JPG within threshold
        best_jpg = None
        best_jpg_date = None
        best_jpg_rank = None
        for fname, ts, ds, _, media_channel, _, _ in jpgs:
            if (
                event_channel is not None and media_channel is not None
                and event_channel != media_channel
            ):
                continue
            dist = abs((ts - ev_ts).total_seconds())
            if dist > MATCH_THRESHOLD_SECS:
                continue
            rank = (
                0 if event_channel is not None and event_channel == media_channel else 1,
                dist,
                ts,
                fname,
                ds,
            )
            if best_jpg_rank is None or rank < best_jpg_rank:
                best_jpg_rank = rank
                best_jpg = fname
                best_jpg_date = ds

        recording = _select_recording(ev_ts, davs, event_channel)
        best_dav = recording[0] if recording is not None else None
        best_dav_date = recording[3] if recording is not None else None

        results.append({
            **ev,
            "snapshot": best_jpg,
            "snapshot_date": best_jpg_date,
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
    """Check whether a cached MP4 matches the current DAV source."""
    mp4_name = Path(dav_filename).stem + ".mp4"
    cached = Path(VIDEO_CACHE_DIR) / date_str / mp4_name
    source = get_media_path(date_str, dav_filename)
    source_fingerprint = get_file_fingerprint(source) if source is not None else None
    return (
        cached.is_file()
        and source_fingerprint is not None
        and _cached_fingerprint(cached) == source_fingerprint
    )


def get_cached_video_path(date_str: str, dav_filename: str) -> Path | None:
    """Return a cached MP4 only when it matches the current DAV source."""
    mp4_name = Path(dav_filename).stem + ".mp4"
    cached = Path(VIDEO_CACHE_DIR) / date_str / mp4_name
    source = get_media_path(date_str, dav_filename)
    source_fingerprint = get_file_fingerprint(source) if source is not None else None
    if (
        cached.is_file()
        and source_fingerprint is not None
        and _cached_fingerprint(cached) == source_fingerprint
    ):
        cached.touch()  # update mtime for LRU
        return cached
    return None


def _fingerprint_path(cached: Path) -> Path:
    return cached.with_name(cached.name + ".source")


def _cached_fingerprint(cached: Path) -> tuple[int, int] | None:
    try:
        size, mtime_ns = _fingerprint_path(cached).read_text(encoding="ascii").split()
        return int(size), int(mtime_ns)
    except (OSError, ValueError):
        return None


def _write_cached_fingerprint(cached: Path, fingerprint: tuple[int, int]) -> bool:
    metadata = _fingerprint_path(cached)
    temporary = metadata.with_name(metadata.name + ".tmp")
    try:
        temporary.write_text(f"{fingerprint[0]} {fingerprint[1]}\n", encoding="ascii")
        temporary.replace(metadata)
        return True
    except OSError:
        logger.exception("Failed to write cache source fingerprint: %s", metadata)
        temporary.unlink(missing_ok=True)
        return False


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
    source = get_media_path(date_str, dav_filename)
    if source is None:
        logger.warning("DAV source not found or unsafe: %s/%s", date_str, dav_filename)
        return None

    lock = _get_conversion_lock(f"{date_str}/{dav_filename}")
    with lock:
        cached = get_cached_video_path(date_str, dav_filename)
        if cached is not None:
            return cached

        source_fingerprint = wait_for_stable_file(source)
        if source_fingerprint is None:
            logger.warning("DAV source is still growing or unavailable: %s", source)
            return None

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

        if get_file_fingerprint(source) != source_fingerprint:
            logger.warning("DAV source changed during conversion, discarding output: %s", source)
            tmp_output.unlink(missing_ok=True)
            return None

        tmp_output.rename(output)
        if not _write_cached_fingerprint(output, source_fingerprint):
            output.unlink(missing_ok=True)
            return None
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
                _fingerprint_path(oldest).unlink(missing_ok=True)
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

    source = get_media_path(date_str, filename)
    if source is None:
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
