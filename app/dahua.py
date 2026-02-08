"""
Dahua camera event listener.

Connects to the camera's eventManager.cgi endpoint via HTTP Digest Auth
and streams CrossLineDetection (or other IVS) events in real time.
Each qualifying event is stored in the database as one car passing.
"""

import json
import logging
import os
import threading

import requests
from requests.auth import HTTPDigestAuth

from app.database import insert_event

logger = logging.getLogger(__name__)

EVENT_URL_TEMPLATE = (
    "{protocol}://{host}:{port}"
    "/cgi-bin/eventManager.cgi?action=attach&codes=[{events}]"
)

# Reconnect timing
INITIAL_BACKOFF = 2  # seconds
MAX_BACKOFF = 60


class DahuaListener:
    """Background thread that subscribes to Dahua camera events."""

    def __init__(
        self,
        host: str,
        port: int = 80,
        user: str = "admin",
        password: str = "admin",
        events: str = "CrossLineDetection",
        protocol: str = "http",
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.events = events
        self.protocol = protocol
        self.url = EVENT_URL_TEMPLATE.format(
            protocol=protocol,
            host=host,
            port=port,
            events=events,
        )
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # -- public API -----------------------------------------------------------

    def start(self):
        """Start listening in a daemon thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("Listener already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("Dahua listener started for %s", self.url)

    def stop(self):
        """Signal the listener to stop."""
        self._stop_event.set()
        logger.info("Dahua listener stop requested")

    # -- internals ------------------------------------------------------------

    def _run(self):
        """Main loop: connect, stream, reconnect on failure."""
        backoff = INITIAL_BACKOFF
        while not self._stop_event.is_set():
            try:
                logger.info("Connecting to %s", self.url)
                resp = requests.get(
                    self.url,
                    stream=True,
                    auth=HTTPDigestAuth(self.user, self.password),
                    timeout=(10, None),  # 10s connect, no read timeout
                )
                resp.raise_for_status()
                logger.info("Connected to Dahua camera at %s:%s", self.host, self.port)
                backoff = INITIAL_BACKOFF  # reset on success

                self._consume_stream(resp)

            except requests.ConnectionError as exc:
                logger.error("Connection error: %s", exc)
            except requests.HTTPError as exc:
                logger.error("HTTP error: %s", exc)
            except Exception as exc:
                logger.exception("Unexpected error in event listener: %s", exc)

            if not self._stop_event.is_set():
                logger.info("Reconnecting in %ds ...", backoff)
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)

    def _consume_stream(self, resp: requests.Response):
        """
        Read the multipart event stream line by line.

        Dahua cameras send data like:
            Code=CrossLineDetection;action=Start;index=0;data={...}
        We only care about action=Start events.
        """
        buffer = ""
        for chunk in resp.iter_content(chunk_size=1024):
            if self._stop_event.is_set():
                break
            if chunk is None:
                continue

            buffer += chunk.decode("utf-8", errors="ignore") if isinstance(chunk, bytes) else chunk
            # Process complete lines
            while "\r\n" in buffer:
                line, buffer = buffer.split("\r\n", 1)
                self._process_line(line)

    def _process_line(self, line: str):
        """Parse a single event line from the stream."""
        if not line.startswith("Code="):
            return

        alarm = {}
        for kv in line.split(";"):
            if "=" not in kv:
                continue
            key, value = kv.split("=", 1)
            alarm[key.strip()] = value.strip()

        code = alarm.get("Code", "")
        action = alarm.get("action", "")

        # Only count the Start of each event (not Stop/Pulse)
        if action != "Start":
            return

        direction = ""
        if "data" in alarm:
            try:
                data = json.loads(alarm["data"])
                direction = data.get("Direction", "")
            except (json.JSONDecodeError, TypeError):
                pass

        camera_name = f"{self.host}"
        logger.info(
            "Event: code=%s direction=%s camera=%s", code, direction, camera_name
        )
        insert_event(camera=camera_name, direction=direction)


def create_listener_from_env() -> DahuaListener:
    """Build a DahuaListener from environment variables."""
    host = os.environ.get("DAHUA_HOST", "")
    if not host:
        logger.warning("DAHUA_HOST not set -- listener will not start")
        return None  # type: ignore[return-value]

    return DahuaListener(
        host=host,
        port=int(os.environ.get("DAHUA_PORT", "80")),
        user=os.environ.get("DAHUA_USER", "admin"),
        password=os.environ.get("DAHUA_PASS", "admin"),
        events=os.environ.get("DAHUA_EVENTS", "CrossLineDetection"),
        protocol=os.environ.get("DAHUA_PROTOCOL", "http"),
    )
