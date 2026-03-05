"""
Background analysis of intrusion event snapshots using a local Ollama vision model.

Events are queued after registration; a single worker thread polls for the
snapshot file, sends it to Ollama, and stores the result in the database.
"""

import base64
import io
import logging
import os
import queue
import threading
import time
from pathlib import Path

import httpx
from PIL import Image

from app.database import (
    create_pending_analysis,
    get_event_by_id,
    get_intrusion_event_ids_without_analysis,
    update_analysis,
)
from app.intrusions import MEDIA_PATH, match_media_for_events

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://ollama:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "moondream")
OLLAMA_PROMPT = os.environ.get(
    "OLLAMA_PROMPT",
    "You are analyzing a security camera snapshot from an intrusion detection event. "
    "Describe concisely what you see in the image: people, vehicles, animals, or other. "
    "Note anything that might explain the alarm (e.g. person, animal, lighting). "
    "Keep the response to a few short sentences.",
)
ANALYSIS_SNAPSHOT_WAIT = int(os.environ.get("ANALYSIS_SNAPSHOT_WAIT", "120"))
OLLAMA_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "600"))
# Max width (px) for snapshots sent to the LLM; larger images are downscaled.
ANALYSIS_SNAPSHOT_MAX_WIDTH = int(os.environ.get("ANALYSIS_SNAPSHOT_MAX_WIDTH", "1024"))


class AnalysisWorker:
    """Single-threaded worker that processes intrusion events for LLM analysis."""

    def __init__(self):
        self._queue: queue.Queue[int] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def get_queue_size(self) -> int:
        """Return approximate number of event IDs waiting in the in-memory queue."""
        return self._queue.qsize()

    def enqueue(self, event_id: int) -> None:
        """Schedule an intrusion event for analysis. Non-blocking."""
        try:
            create_pending_analysis(event_id)
        except Exception as e:
            event = get_event_by_id(event_id)
            ts = event["timestamp"] if event else "?"
            logger.warning("Could not create pending analysis for event %s (%s): %s", event_id, ts, e)
            return
        self._queue.put_nowait(event_id)
        event = get_event_by_id(event_id)
        ts = event["timestamp"] if event else "?"
        logger.debug("Enqueued event %s (%s) for analysis", event_id, ts)

    def start(self) -> None:
        """Start the worker thread (daemon)."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("[AI] Analysis worker already running")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("[AI] Analysis worker started (Ollama: %s, model: %s)", OLLAMA_HOST, OLLAMA_MODEL)
        self._backfill()

    def stop(self) -> None:
        """Signal the worker to stop."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        logger.info("[AI] Analysis worker stopped")

    def _backfill(self) -> None:
        """Queue intrusion events from the last 7 days that have no analysis (no catch-up for older)."""
        try:
            ids = get_intrusion_event_ids_without_analysis(max_age_days=7)
            for event_id in ids:
                create_pending_analysis(event_id)
                self._queue.put_nowait(event_id)
            if ids:
                logger.info("[AI] Backfill: queued %d intrusion event(s) for analysis", len(ids))
        except Exception as e:
            logger.exception("[AI] Backfill failed: %s", e)

    def _run(self) -> None:
        """Process the queue until stopped."""
        while not self._stop.is_set():
            try:
                event_id = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            self._process_one(event_id)

    def _process_one(self, event_id: int) -> None:
        """Process a single event: wait for snapshot, call Ollama, store result."""
        event = get_event_by_id(event_id)
        if event is None or event.get("event_type") != "intrusion":
            ts = event["timestamp"] if event else "?"
            logger.debug("[AI] Event %s (%s) not found or not intrusion, skipping", event_id, ts)
            return

        timestamp = event["timestamp"]
        date_str = timestamp[:10]
        ev = [{"id": event_id, "timestamp": timestamp}]

        snapshot_path = None
        snapshot_date = None
        deadline = time.monotonic() + ANALYSIS_SNAPSHOT_WAIT
        while time.monotonic() < deadline and not self._stop.is_set():
            matched = match_media_for_events(ev, date_str)
            if matched and matched[0].get("snapshot") and matched[0].get("snapshot_date"):
                snapshot_path = Path(MEDIA_PATH) / matched[0]["snapshot_date"] / matched[0]["snapshot"]
                if snapshot_path.is_file():
                    snapshot_date = matched[0]["snapshot_date"]
                    break
            time.sleep(2)

        if snapshot_path is None or not snapshot_path.is_file():
            logger.warning("[AI] No snapshot found for event %s (%s) within %ds", event_id, timestamp, ANALYSIS_SNAPSHOT_WAIT)
            update_analysis(event_id, "failed", analysis=None, model=None)
            return

        try:
            with Image.open(snapshot_path) as img:
                img.load()
                w, h = img.size
                if w > ANALYSIS_SNAPSHOT_MAX_WIDTH:
                    ratio = ANALYSIS_SNAPSHOT_MAX_WIDTH / w
                    new_w = ANALYSIS_SNAPSHOT_MAX_WIDTH
                    new_h = int(h * ratio)
                    img = img.resize((new_w, new_h), Image.LANCZOS)
                    logger.debug("[AI] Resized snapshot for LLM: %dx%d -> %dx%d", w, h, new_w, new_h)
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=85, optimize=True)
                image_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        except OSError as e:
            logger.warning("[AI] Cannot read snapshot for event %s (%s): %s", event_id, timestamp, e)
            update_analysis(event_id, "failed", analysis=None, model=None)
            return
        except Exception as e:
            logger.warning("[AI] Cannot process snapshot for event %s (%s): %s", event_id, timestamp, e)
            update_analysis(event_id, "failed", analysis=None, model=None)
            return

        payload = {
            "model": OLLAMA_MODEL,
            "stream": False,
            "messages": [
                {
                    "role": "user",
                    "content": OLLAMA_PROMPT,
                    "images": [image_b64],
                }
            ],
        }

        t0 = time.monotonic()
        try:
            with httpx.Client(timeout=OLLAMA_TIMEOUT) as client:
                resp = client.post(f"{OLLAMA_HOST.rstrip('/')}/api/chat", json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            logger.warning("[AI] Ollama API error for event %s (%s): %s %s", event_id, timestamp, e.response.status_code, e.response.text)
            update_analysis(event_id, "failed", analysis=None, model=None)
            return
        except Exception as e:
            logger.exception("[AI] Ollama request failed for event %s (%s): %s", event_id, timestamp, e)
            update_analysis(event_id, "failed", analysis=None, model=None)
            return
        elapsed = time.monotonic() - t0

        message = data.get("message") or {}
        content = message.get("content") or ""
        model_used = data.get("model") or OLLAMA_MODEL
        update_analysis(event_id, "done", analysis=content.strip() or None, model=model_used)
        logger.info("[AI] Analysis done for event %s (%s) (model: %s, %.1fs)", event_id, timestamp, model_used, elapsed)
