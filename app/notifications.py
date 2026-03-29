"""
Push notifications for intrusion events via ntfy.sh.

Sends a single notification immediately when an intrusion event is registered,
with a click-through link to the event in the web dashboard.

Disabled when NTFY_TOPIC is empty (the default).
"""

import logging
import os
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "").strip()
NTFY_PRIORITY = os.environ.get("NTFY_PRIORITY", "default").strip()
NTFY_CLICK_URL = os.environ.get("NTFY_CLICK_URL", "").strip().rstrip("/")

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


def send_intrusion_notification(event_id: int, timestamp: str) -> None:
    """Send an intrusion notification immediately.

    Includes a click-through link to the event in the web dashboard
    when NTFY_CLICK_URL is configured.
    """
    if not _enabled():
        return

    title = _make_title(timestamp)
    headers = {
        **_auth_headers(),
        "Title": title,
        "Priority": NTFY_PRIORITY,
        "Tags": "rotating_light",
    }

    if NTFY_CLICK_URL:
        headers["Click"] = f"{NTFY_CLICK_URL}/#/event/{event_id}"

    url = f"{NTFY_URL}/{NTFY_TOPIC}"

    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(
                url,
                content=b"Intrusion detected",
                headers=headers,
            )
            resp.raise_for_status()
        logger.info("[ntfy] Sent notification for event %s", event_id)
    except Exception:
        logger.exception("[ntfy] Failed to send notification for event %s", event_id)
