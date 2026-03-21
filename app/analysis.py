"""
Background analysis of intrusion event videos using a local Ollama vision model.

Events are queued in memory after registration; a single worker thread waits
for the video recording to finish uploading, extracts motion-significant frames,
sends them to Ollama, and stores the result in the database.
Queue is repopulated on startup from unprocessed events in the last 7 days.
"""

import base64
import io
import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
from PIL import Image, ImageChops

from app.database import (
    get_event_by_id,
    get_intrusion_event_ids_without_analysis,
    update_analysis,
)
from app.intrusions import MEDIA_PATH, match_media_for_events
from app.notifications import update_intrusion_notification

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://ollama:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3-vl:8b")
OLLAMA_PROMPT = os.environ.get(
    "OLLAMA_PROMPT",
    "You are analyzing frames extracted from a security camera video of an "
    "intrusion detection event. Describe concisely what you see across the "
    "frames: people, vehicles, animals, movement patterns, or other notable "
    "activity. Keep the response to a few short sentences. "
    "Ignore weather conditions and overlay timestamp.",
)
OLLAMA_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "600"))
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))

ANALYSIS_VIDEO_WAIT = int(os.environ.get("ANALYSIS_VIDEO_WAIT", "300"))
ANALYSIS_FRAME_WIDTH = int(os.environ.get("ANALYSIS_FRAME_WIDTH", "512"))
ANALYSIS_MOTION_THRESHOLD = float(os.environ.get("ANALYSIS_MOTION_THRESHOLD", "0.015"))
ANALYSIS_MOTION_SAMPLE_RATE = float(os.environ.get("ANALYSIS_MOTION_SAMPLE_RATE", "0.5"))
ANALYSIS_MOTION_MASK = os.environ.get("ANALYSIS_MOTION_MASK", "")

_DEFAULT_MASK_PATH = Path(__file__).parent / "masks" / "maska.png"


# ---------------------------------------------------------------------------
# Frame extraction (motion-based)
# ---------------------------------------------------------------------------


def _run_ffmpeg(cmd: list[str], timeout: int = 120) -> bool:
    """Run an ffmpeg command. Returns True on success."""
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
        return True
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"")[-500:].decode("utf-8", errors="replace")
        logger.warning("[AI] ffmpeg error: %s", stderr)
    except subprocess.TimeoutExpired:
        logger.warning("[AI] ffmpeg timed out after %ds", timeout)
    return False


def _load_motion_mask(mask_path: Path) -> np.ndarray | None:
    """Load a grayscale mask and return a boolean ndarray (True=monitor)."""
    if not mask_path.is_file():
        logger.warning("[AI] Motion mask not found: %s", mask_path)
        return None
    try:
        mask_img = Image.open(mask_path).convert("L")
        mask_img.load()
        mask_bool = np.asarray(mask_img) > 127
        active = int(mask_bool.sum())
        total = mask_bool.size
        logger.info(
            "[AI] Motion mask loaded: %s (%dx%d, %d/%d active pixels)",
            mask_path, mask_img.size[0], mask_img.size[1], active, total,
        )
        return mask_bool
    except Exception as e:
        logger.error("[AI] Cannot load motion mask %s: %s", mask_path, e)
        return None


def _compute_frame_diff(
    img1: Image.Image,
    img2: Image.Image,
    mask_bool: np.ndarray | None = None,
) -> float:
    """Return normalised mean pixel difference (0.0-1.0) between two images.

    If *mask_bool* is provided (boolean ndarray, True=monitor, False=ignore),
    only True pixels contribute to the mean.  Must match frame dimensions;
    a mismatch is logged and the mask is ignored for that comparison.
    """
    g1 = img1.convert("L")
    g2 = img2.convert("L")
    if g1.size != g2.size:
        g2 = g2.resize(g1.size, Image.LANCZOS)

    diff = ImageChops.difference(g1, g2)

    if mask_bool is not None:
        diff_arr = np.asarray(diff, dtype=np.float32)
        if diff_arr.shape != mask_bool.shape:
            logger.error(
                "[AI] Mask size %dx%d does not match frame size %dx%d",
                mask_bool.shape[1], mask_bool.shape[0], g1.size[0], g1.size[1],
            )
        else:
            masked = diff_arr[mask_bool]
            if masked.size == 0:
                return 0.0
            return float(masked.mean() / 255.0)

    hist = diff.histogram()
    total_pixels = g1.size[0] * g1.size[1]
    mean_diff = sum(i * count for i, count in enumerate(hist)) / total_pixels
    return mean_diff / 255.0


def _extract_frames_motion(
    video_path: Path,
    out_dir: Path,
    threshold: float,
    sample_rate: float,
    *,
    width: int | None = None,
    mask_bool: np.ndarray | None = None,
) -> list[Path]:
    """Extract frames where pixel-level change exceeds *threshold*.

    Candidates are sampled every *sample_rate* seconds and optionally scaled
    to *width* via ffmpeg (resize-first workflow: frames are already at target
    resolution for both motion detection and LLM).

    If *mask_bool* is provided, only True pixels contribute to the diff.
    The mask must match the (scaled) frame dimensions exactly.
    """
    candidates_dir = out_dir / "_candidates"
    candidates_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(candidates_dir / "cand_%06d.jpg")
    vf = f"scale={width}:-2,fps=1/{sample_rate}" if width else f"fps=1/{sample_rate}"
    cmd = [
        "ffmpeg", "-i", str(video_path),
        "-vf", vf,
        "-q:v", "2",
        pattern,
    ]
    if not _run_ffmpeg(cmd):
        return []

    candidate_paths = sorted(candidates_dir.glob("cand_*.jpg"))
    if not candidate_paths:
        shutil.rmtree(candidates_dir, ignore_errors=True)
        return []

    kept: list[Path] = []
    ref_img: Image.Image | None = None
    frame_idx = 0

    for cp in candidate_paths:
        try:
            img = Image.open(cp)
            img.load()
        except Exception as e:
            logger.debug("[AI] Cannot open candidate frame %s: %s", cp.name, e)
            continue

        if ref_img is None:
            dst = out_dir / f"frame_{frame_idx:04d}.jpg"
            cp.rename(dst)
            kept.append(dst)
            ref_img = img
            frame_idx += 1
            continue

        diff = _compute_frame_diff(ref_img, img, mask_bool=mask_bool)
        if diff >= threshold:
            dst = out_dir / f"frame_{frame_idx:04d}.jpg"
            cp.rename(dst)
            kept.append(dst)
            ref_img = img
            frame_idx += 1

    mask_label = " +mask" if mask_bool is not None else ""
    width_label = f" @{width}px" if width else ""
    logger.debug(
        "[AI] Motion filter: %d candidates -> %d kept "
        "(threshold=%.3f, sample_rate=%.2fs%s%s)",
        len(candidate_paths), len(kept), threshold, sample_rate,
        width_label, mask_label,
    )
    shutil.rmtree(candidates_dir, ignore_errors=True)
    return kept


def _load_and_encode_frames(frame_paths: list[Path]) -> tuple[list[str], int]:
    """Load already-scaled frames, JPEG-encode, return (base64 list, total bytes)."""
    encoded: list[str] = []
    total_bytes = 0

    for fp in frame_paths:
        try:
            with Image.open(fp) as img:
                img.load()
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")

                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=85, optimize=True)
                data = buf.getvalue()
                total_bytes += len(data)
                encoded.append(base64.b64encode(data).decode("ascii"))
        except Exception as e:
            logger.debug("[AI] Failed to process frame %s: %s", fp.name, e)

    return encoded, total_bytes


# ---------------------------------------------------------------------------
# Temporary DAV-to-MP4 conversion for frame extraction
# ---------------------------------------------------------------------------


def _convert_dav_to_mp4_temp(dav_path: Path, output_dir: Path) -> Path | None:
    """Convert a DAV file to MP4 in *output_dir* using ffmpeg."""
    mp4_path = output_dir / (dav_path.stem + ".mp4")
    cmd = [
        "ffmpeg", "-y", "-i", str(dav_path),
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-an",
        "-movflags", "+faststart",
        str(mp4_path),
    ]
    if _run_ffmpeg(cmd, timeout=600):
        return mp4_path
    mp4_path.unlink(missing_ok=True)
    return None


# ---------------------------------------------------------------------------
# Analysis worker
# ---------------------------------------------------------------------------


class AnalysisWorker:
    """Single-threaded worker that processes intrusion events for LLM analysis.

    Queue is kept in memory only; on startup it is filled from events without
    analysis in the last 7 days.
    """

    def __init__(self):
        self._queue: queue.Queue[int] = queue.Queue()
        self._queue_contents: list[dict] = []
        self._queue_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._motion_mask_bool: np.ndarray | None = None

    def get_queue_size(self) -> int:
        with self._queue_lock:
            return len(self._queue_contents)

    def get_queue_contents(self) -> list[dict]:
        with self._queue_lock:
            return [dict(item) for item in self._queue_contents]

    def enqueue(self, event_id: int) -> None:
        """Schedule an intrusion event for analysis. Non-blocking."""
        with self._queue_lock:
            if any(item["event_id"] == event_id for item in self._queue_contents):
                return
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            self._queue_contents.append({"event_id": event_id, "created_at": now})
        self._queue.put_nowait(event_id)
        event = get_event_by_id(event_id)
        ts = event["timestamp"] if event else "?"
        logger.info("[AI] Enqueued event %s (%s) for analysis", event_id, ts)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            logger.warning("[AI] Analysis worker already running")
            return

        mask_path_str = ANALYSIS_MOTION_MASK.strip()
        if mask_path_str and mask_path_str.lower() not in ("none", "off"):
            self._motion_mask_bool = _load_motion_mask(Path(mask_path_str))
        elif not mask_path_str:
            self._motion_mask_bool = _load_motion_mask(_DEFAULT_MASK_PATH)

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("[AI] Analysis worker started (Ollama: %s, model: %s)", OLLAMA_HOST, OLLAMA_MODEL)
        self._backfill()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        logger.info("[AI] Analysis worker stopped")

    def _backfill(self) -> None:
        """Queue intrusion events from the last 7 days that have no analysis."""
        try:
            ids = get_intrusion_event_ids_without_analysis(max_age_days=7)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            with self._queue_lock:
                for event_id in ids:
                    if any(item["event_id"] == event_id for item in self._queue_contents):
                        continue
                    self._queue_contents.append({"event_id": event_id, "created_at": now})
                    self._queue.put_nowait(event_id)
            if ids:
                logger.info("[AI] Backfill: queued %d intrusion event(s) for analysis", len(ids))
        except Exception as e:
            logger.exception("[AI] Backfill failed: %s", e)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                event_id = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            with self._queue_lock:
                self._queue_contents[:] = [x for x in self._queue_contents if x["event_id"] != event_id]
            self._process_one(event_id)

    def _process_one(self, event_id: int) -> None:
        """Process a single event: wait for video, extract frames, call Ollama."""
        event = get_event_by_id(event_id)
        if event is None or event.get("event_type") != "intrusion":
            logger.debug("[AI] Event %s not found or not intrusion, skipping", event_id)
            return

        timestamp = event["timestamp"]
        date_str = timestamp[:10]
        ev = [{"id": event_id, "timestamp": timestamp}]

        # Wait for the video recording (DAV file) to appear on disk
        video_path = None
        video_date = None
        snapshot_path = None
        deadline = time.monotonic() + ANALYSIS_VIDEO_WAIT
        while time.monotonic() < deadline and not self._stop.is_set():
            matched = match_media_for_events(ev, date_str)
            if matched:
                m = matched[0]
                if m.get("snapshot") and m.get("snapshot_date"):
                    snapshot_path = Path(MEDIA_PATH) / m["snapshot_date"] / m["snapshot"]
                    if not snapshot_path.is_file():
                        snapshot_path = None
                if m.get("video") and m.get("video_date"):
                    candidate = Path(MEDIA_PATH) / m["video_date"] / m["video"]
                    if candidate.is_file():
                        video_path = candidate
                        video_date = m["video_date"]
                        break
            time.sleep(2)

        if video_path is None:
            logger.warning(
                "[AI] No video found for event %s (%s) within %ds",
                event_id, timestamp, ANALYSIS_VIDEO_WAIT,
            )
            update_analysis(event_id, "failed", analysis=None, model=None)
            update_intrusion_notification(event_id, timestamp, None, snapshot_path)
            return

        logger.info("[AI] Processing event %s (%s) — video: %s", event_id, timestamp, video_path.name)

        work_dir = None
        try:
            work_dir = Path(tempfile.mkdtemp(prefix="analysis_"))

            # Convert DAV to MP4 if needed
            if video_path.suffix.lower() == ".dav":
                mp4_path = _convert_dav_to_mp4_temp(video_path, work_dir)
                if mp4_path is None:
                    logger.warning("[AI] DAV conversion failed for event %s (%s)", event_id, timestamp)
                    update_analysis(event_id, "failed", analysis=None, model=None)
                    update_intrusion_notification(event_id, timestamp, None, snapshot_path)
                    return
            else:
                mp4_path = video_path

            # Extract motion-significant frames (resized to target width by ffmpeg)
            frame_dir = work_dir / "frames"
            frame_dir.mkdir()
            frames = _extract_frames_motion(
                mp4_path, frame_dir,
                threshold=ANALYSIS_MOTION_THRESHOLD,
                sample_rate=ANALYSIS_MOTION_SAMPLE_RATE,
                width=ANALYSIS_FRAME_WIDTH,
                mask_bool=self._motion_mask_bool,
            )

            if not frames:
                logger.warning("[AI] No frames extracted for event %s (%s)", event_id, timestamp)
                update_analysis(event_id, "failed", analysis=None, model=None)
                update_intrusion_notification(event_id, timestamp, None, snapshot_path)
                return

            images_b64, total_bytes = _load_and_encode_frames(frames)
            logger.info(
                "[AI] Event %s: %d frames, %.0f KB image data",
                event_id, len(images_b64), total_bytes / 1024,
            )

            if not images_b64:
                logger.warning("[AI] All frames failed to encode for event %s (%s)", event_id, timestamp)
                update_analysis(event_id, "failed", analysis=None, model=None)
                update_intrusion_notification(event_id, timestamp, None, snapshot_path)
                return

            # Call Ollama
            payload: dict = {
                "model": OLLAMA_MODEL,
                "stream": False,
                "messages": [
                    {
                        "role": "user",
                        "content": OLLAMA_PROMPT,
                        "images": images_b64,
                    }
                ],
                "options": {"num_ctx": OLLAMA_NUM_CTX},
            }

            t0 = time.monotonic()
            try:
                with httpx.Client(timeout=OLLAMA_TIMEOUT) as client:
                    resp = client.post(f"{OLLAMA_HOST.rstrip('/')}/api/chat", json=payload)
                    resp.raise_for_status()
                    data = resp.json()
            except httpx.HTTPStatusError as e:
                logger.warning(
                    "[AI] Ollama API error for event %s (%s): %s %s",
                    event_id, timestamp, e.response.status_code, e.response.text[:200],
                )
                update_analysis(event_id, "failed", analysis=None, model=None)
                update_intrusion_notification(event_id, timestamp, None, snapshot_path)
                return
            except Exception as e:
                logger.exception("[AI] Ollama request failed for event %s (%s): %s", event_id, timestamp, e)
                update_analysis(event_id, "failed", analysis=None, model=None)
                update_intrusion_notification(event_id, timestamp, None, snapshot_path)
                return
            elapsed = time.monotonic() - t0

            message = data.get("message") or {}
            content = message.get("content") or ""
            model_used = data.get("model") or OLLAMA_MODEL
            eval_count = data.get("eval_count") or "?"

            analysis_text = content.strip() or None
            update_analysis(event_id, "done", analysis=analysis_text, model=model_used)
            update_intrusion_notification(event_id, timestamp, analysis_text, snapshot_path)
            logger.info(
                "[AI] Analysis done for event %s (%s) — model: %s, %.1fs, %s frames, %s tokens",
                event_id, timestamp, model_used, elapsed, len(images_b64), eval_count,
            )

        except Exception as e:
            logger.exception("[AI] Unexpected error analysing event %s (%s): %s", event_id, timestamp, e)
            update_analysis(event_id, "failed", analysis=None, model=None)
            update_intrusion_notification(event_id, timestamp, None, snapshot_path)
        finally:
            if work_dir is not None:
                shutil.rmtree(work_dir, ignore_errors=True)
