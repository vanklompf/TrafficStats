"""
TrafficStats -- FastAPI application.

Serves the dashboard and provides an API for traffic event statistics
and intrusion event browsing with snapshot/video media.
On startup, initialises the database and launches the Dahua event listener.
"""

import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.database import init_db, close_conn, get_stats, get_intrusion_events, get_intrusion_dates
from app.dahua import DahuaListener, create_listener_from_env
from app.intrusions import (
    MEDIA_PATH,
    match_media_for_events,
    convert_dav_to_mp4,
    get_cached_video_path,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_listener: DahuaListener | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown hooks."""
    global _listener

    # Initialise database
    init_db()

    # Start Dahua event listener
    _listener = create_listener_from_env()
    if _listener is not None:
        _listener.start()
    else:
        logger.warning("No DAHUA_HOST configured -- running without camera listener")

    yield

    # Shutdown
    if _listener is not None:
        _listener.stop()
    close_conn()


app = FastAPI(title="TrafficStats", lifespan=lifespan)

# Mount static files (CSS/JS assets if any)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
async def index():
    """Serve the dashboard page."""
    return FileResponse("app/static/index.html")


@app.get("/api/stats")
async def api_stats(range: str = Query("24h", pattern="^(24h|week)$")):
    """
    Return traffic event counts in 5-minute buckets.

    Query params:
        range: '24h' (default) or 'week'
    """
    data = get_stats(range)
    return JSONResponse(content=data)


@app.get("/api/health")
async def health():
    """Simple health-check endpoint."""
    camera_connected = _listener is not None and _listener.is_alive()
    return {
        "status": "ok",
        "camera_listener": "running" if camera_connected else "stopped",
    }


# ---------------------------------------------------------------------------
# Intrusion routes
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SAFE_FILENAME_RE = re.compile(r"^[\w\.\-\[\]@]+$")


def _validate_date(date_str: str) -> str:
    if not _DATE_RE.match(date_str):
        raise HTTPException(status_code=400, detail="Invalid date format")
    return date_str


def _validate_filename(filename: str) -> str:
    if not _SAFE_FILENAME_RE.match(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    return filename


@app.get("/api/intrusions/dates")
async def api_intrusion_dates():
    """Return list of dates that have intrusion events."""
    return JSONResponse(content={"dates": get_intrusion_dates()})


@app.get("/api/intrusions")
async def api_intrusions(date: str = Query(default="")):
    """
    Return intrusion events for a given date with matched media.

    If no date given, defaults to today (UTC).
    """
    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    date = _validate_date(date)
    events = get_intrusion_events(date)
    enriched = match_media_for_events(events, date)

    # Build URLs for each event's media (file may live in an adjacent
    # date directory when the camera timezone differs from UTC).
    for ev in enriched:
        snap_date = ev.get("snapshot_date") or date
        vid_date = ev.get("video_date") or date
        if ev["snapshot"]:
            ev["snapshot_url"] = f"/media/snapshot/{snap_date}/{ev['snapshot']}"
        else:
            ev["snapshot_url"] = None
        if ev["video"]:
            ev["video_url"] = f"/media/video/{vid_date}/{ev['video']}"
        else:
            ev["video_url"] = None

    return JSONResponse(content={"date": date, "events": enriched})


@app.get("/media/snapshot/{date_str}/{filename}")
async def media_snapshot(date_str: str, filename: str):
    """Serve a JPG snapshot from the media directory."""
    date_str = _validate_date(date_str)
    filename = _validate_filename(filename)
    path = Path(MEDIA_PATH) / date_str / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return FileResponse(str(path), media_type="image/jpeg")


@app.get("/media/video/{date_str}/{filename}")
async def media_video(date_str: str, filename: str):
    """
    Serve a DAV recording as a browser-friendly MP4.

    Converts on first request and caches the result.
    """
    date_str = _validate_date(date_str)
    filename = _validate_filename(filename)

    # Check cache first
    cached = get_cached_video_path(date_str, filename)
    if cached is not None:
        return FileResponse(str(cached), media_type="video/mp4")

    # Convert DAV -> MP4
    mp4_path = convert_dav_to_mp4(date_str, filename)
    if mp4_path is None:
        raise HTTPException(status_code=404, detail="Video not found or conversion failed")
    return FileResponse(str(mp4_path), media_type="video/mp4")
