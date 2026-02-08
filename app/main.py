"""
TrafficStats -- FastAPI application.

Serves the dashboard and provides an API for traffic event statistics.
On startup, initialises the database and launches the Dahua event listener.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.database import init_db, get_stats
from app.dahua import DahuaListener, create_listener_from_env

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
    camera_connected = _listener is not None and (
        _listener._thread is not None and _listener._thread.is_alive()
    )
    return {
        "status": "ok",
        "camera_listener": "running" if camera_connected else "stopped",
    }
