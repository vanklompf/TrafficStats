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

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.database import init_db, close_conn, get_stats, get_intrusion_events, get_intrusion_dates
from app.dahua import DahuaListener, create_listener_from_env
from app.intrusions import (
    MEDIA_PATH,
    match_media_for_events,
    convert_dav_to_mp4,
    get_cached_video_path,
    is_video_cached,
    get_or_create_thumbnail,
)

logger = logging.getLogger(__name__)

_listener: DahuaListener | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown hooks."""
    global _listener

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

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
async def api_stats(
    range: str = Query("day", pattern="^(day|week)$"),
    date: str = Query(default=""),
):
    """
    Return traffic event counts in 5-minute buckets.

    Query params:
        range: 'day' (default) or 'week'
        date:  'YYYY-MM-DD' reference date (defaults to today UTC).
    """
    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _validate_date(date)
    data = get_stats(range, date)
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
# Live camera view
# ---------------------------------------------------------------------------


@app.get("/api/live/mjpeg")
async def live_mjpeg(
    subtype: int = Query(default=1, ge=0, le=3),
):
    """Proxy the camera's MJPEG stream (sub-stream by default for lower bandwidth)."""
    if _listener is None:
        raise HTTPException(status_code=503, detail="Camera not configured")

    url = (
        f"{_listener.protocol}://{_listener.host}:{_listener.port}"
        f"/cgi-bin/mjpg/video.cgi?channel=1&subtype={subtype}"
    )

    client = httpx.AsyncClient(
        auth=httpx.DigestAuth(_listener.user, _listener.password),
        timeout=httpx.Timeout(10.0, read=None),
    )
    try:
        req = client.build_request("GET", url)
        resp = await client.send(req, stream=True)
        resp.raise_for_status()
    except Exception as exc:
        await client.aclose()
        logger.error("Failed to connect to camera MJPEG stream: %s", exc)
        raise HTTPException(status_code=502, detail="Cannot connect to camera")

    content_type = resp.headers.get(
        "content-type", "multipart/x-mixed-replace"
    )

    async def relay():
        try:
            async for chunk in resp.aiter_bytes(chunk_size=8192):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(relay(), media_type=content_type)


@app.get("/api/live/snapshot")
async def live_snapshot():
    """Fetch a single JPEG snapshot from the camera."""
    if _listener is None:
        raise HTTPException(status_code=503, detail="Camera not configured")

    url = (
        f"{_listener.protocol}://{_listener.host}:{_listener.port}"
        f"/cgi-bin/snapshot.cgi?channel=1"
    )
    try:
        async with httpx.AsyncClient(
            auth=httpx.DigestAuth(_listener.user, _listener.password),
            timeout=httpx.Timeout(10.0),
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except Exception as exc:
        logger.error("Failed to fetch camera snapshot: %s", exc)
        raise HTTPException(status_code=502, detail="Cannot connect to camera")

    return Response(
        content=resp.content,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


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
            ev["thumbnail_url"] = f"/media/thumbnail/{snap_date}/{ev['snapshot']}"
        else:
            ev["snapshot_url"] = None
            ev["thumbnail_url"] = None
        if ev["video"]:
            ev["video_url"] = f"/media/video/{vid_date}/{ev['video']}"
            ev["video_cached"] = is_video_cached(vid_date, ev["video"])
        else:
            ev["video_url"] = None
            ev["video_cached"] = False

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


@app.get("/media/thumbnail/{date_str}/{filename}")
async def media_thumbnail(date_str: str, filename: str):
    """Serve a cached, downscaled thumbnail for a snapshot."""
    date_str = _validate_date(date_str)
    filename = _validate_filename(filename)
    thumb = get_or_create_thumbnail(date_str, filename)
    if thumb is None:
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(
        str(thumb),
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


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
