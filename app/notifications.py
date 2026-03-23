"""
Push notifications for intrusion events via ntfy.sh.

Two-phase notification using ntfy sequence IDs:
  1. send_intrusion_notification  -- fires on event registration, waits
     briefly for the camera snapshot and attaches it when available.
  2. update_intrusion_notification -- fires after Ollama analysis, replaces
     the initial notification with the AI description (+ snapshot).

Disabled when NTFY_TOPIC is empty (the default).
"""

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "").strip()
NTFY_PRIORITY = os.environ.get("NTFY_PRIORITY", "default").strip()

SNAPSHOT_POLL_INTERVAL = 2
SNAPSHOT_POLL_TIMEOUT = int(os.environ.get("NTFY_SNAPSHOT_WAIT", "10"))

_TIMEOUT = httpx.Timeout(15.0)


def _enabled() -> bool:
    return bool(NTFY_TOPIC)


def _auth_headers() -> dict[str, str]:
    if NTFY_TOKEN:
        return {"Authorization": f"Bearer {NTFY_TOKEN}"}
    return {}


def _make_title(timestamp: str) -> str:
    """Build a notification title from an event UTC timestamp string."""
    try:
        from app.intrusions import _get_local_tz
        utc_dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc,
        )
        local_dt = utc_dt.astimezone(_get_local_tz())
        return f"Intrusion: {local_dt.strftime('%H:%M:%S')}"
    except Exception:
        return f"Intrusion: {timestamp[11:19]}"


def _topic_url(sequence_id: str) -> str:
    return f"{NTFY_URL}/{NTFY_TOPIC}/{sequence_id}"


def _find_snapshot(timestamp: str) -> Path | None:
    """Poll the media directory for a snapshot matching *timestamp*."""
    from app.intrusions import match_media_for_events

    date_str = timestamp[:10]
    ev = [{"id": 0, "timestamp": timestamp}]
    deadline = time.monotonic() + SNAPSHOT_POLL_TIMEOUT

    while time.monotonic() < deadline:
        matched = match_media_for_events(ev, date_str)
        if matched and matched[0].get("snapshot") and matched[0].get("snapshot_date"):
            from app.intrusions import MEDIA_PATH
            path = Path(MEDIA_PATH) / matched[0]["snapshot_date"] / matched[0]["snapshot"]
            if path.is_file():
                return path
        time.sleep(SNAPSHOT_POLL_INTERVAL)
    return None


def _send_with_snapshot(
    url: str,
    headers: dict[str, str],
    snapshot_path: Path,
    message: str,
) -> None:
    """PUT the snapshot JPEG as the request body, message in headers."""
    headers = {
        **headers,
        "Filename": "snapshot.jpg",
        "Message": message,
    }
    with open(snapshot_path, "rb") as f:
        data = f.read()
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.put(url, content=data, headers=headers)
        resp.raise_for_status()


def _send_text(
    url: str,
    headers: dict[str, str],
    message: str,
) -> None:
    """POST a plain-text notification."""
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.post(url, content=message.encode(), headers=headers)
        resp.raise_for_status()


# -- Public API --------------------------------------------------------------


def send_intrusion_notification(event_id: int, timestamp: str) -> None:
    """Send the initial intrusion notification (phase 1).

    Waits up to NTFY_SNAPSHOT_WAIT seconds for the camera snapshot.  If
    available, attaches it; otherwise sends a text-only notification.
    Called from the Dahua listener callback thread.
    """
    if not _enabled():
        return

    seq_id = f"intrusion-{event_id}"
    title = _make_title(timestamp)
    base_headers = {
        **_auth_headers(),
        "Title": title,
        "Priority": NTFY_PRIORITY,
        "Tags": "rotating_light",
    }

    try:
        snapshot = _find_snapshot(timestamp)
        url = _topic_url(seq_id)
        message = "Intrusion detected. Analyzing video\u2026"

        if snapshot is not None:
            _send_with_snapshot(url, base_headers, snapshot, message)
            logger.info("[ntfy] Sent notification for event %s (with snapshot)", event_id)
        else:
            _send_text(url, base_headers, message)
            logger.info("[ntfy] Sent notification for event %s (no snapshot)", event_id)
    except Exception:
        logger.exception("[ntfy] Failed to send notification for event %s", event_id)


def update_intrusion_notification(
    event_id: int,
    timestamp: str,
    analysis: str | None,
    snapshot_path: Path | None,
) -> None:
    """Update the intrusion notification with the analysis result (phase 2).

    Replaces the initial notification via the same ntfy sequence ID.
    Called from the analysis worker thread after Ollama completes (or fails).
    """
    if not _enabled():
        return

    seq_id = f"intrusion-{event_id}"
    title = _make_title(timestamp)
    base_headers = {
        **_auth_headers(),
        "Title": title,
        "Priority": NTFY_PRIORITY,
        "Tags": "rotating_light",
    }

    message = analysis if analysis else "Intrusion detected (analysis unavailable)."

    try:
        url = _topic_url(seq_id)
        if snapshot_path is not None and snapshot_path.is_file():
            _send_with_snapshot(url, base_headers, snapshot_path, message)
            logger.info("[ntfy] Updated notification for event %s (with snapshot)", event_id)
        else:
            _send_text(url, base_headers, message)
            logger.info("[ntfy] Updated notification for event %s (text only)", event_id)
    except Exception:
        logger.exception("[ntfy] Failed to update notification for event %s", event_id)
