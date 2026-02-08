import sqlite3
import threading
import os
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/data/traffic.db")

_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """Get a thread-local SQLite connection."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _local.conn = conn
    return conn


def init_db():
    """Create the events table if it doesn't exist."""
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            camera TEXT NOT NULL DEFAULT '',
            direction TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_events_timestamp
        ON events (timestamp)
    """)
    conn.commit()
    logger.info("Database initialised at %s", DB_PATH)


def insert_event(camera: str = "", direction: str = ""):
    """Insert a single car-passing event with the current UTC timestamp."""
    conn = _get_conn()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO events (timestamp, camera, direction) VALUES (?, ?, ?)",
        (now, camera, direction),
    )
    conn.commit()
    logger.info("Event recorded: camera=%s direction=%s at %s", camera, direction, now)


def get_stats(range_key: str) -> dict:
    """
    Return event counts aggregated into 5-minute buckets.

    range_key: '24h' or 'week'
    Returns: {'buckets': [{'time': '...', 'count': N}, ...], 'total': N}
    """
    conn = _get_conn()

    if range_key == "week":
        since = datetime.now(timezone.utc) - timedelta(days=7)
    else:
        since = datetime.now(timezone.utc) - timedelta(hours=24)

    since_str = since.strftime("%Y-%m-%d %H:%M:%S")

    # Group into 5-minute buckets by truncating minutes to nearest 5
    rows = conn.execute(
        """
        SELECT
            strftime('%%Y-%%m-%%d %%H:', timestamp)
                || printf('%%02d', (CAST(strftime('%%M', timestamp) AS INTEGER) / 5) * 5)
                AS bucket,
            COUNT(*) AS count
        FROM events
        WHERE timestamp >= ?
        GROUP BY bucket
        ORDER BY bucket
        """,
        (since_str,),
    ).fetchall()

    buckets = [{"time": row["bucket"], "count": row["count"]} for row in rows]
    total = sum(b["count"] for b in buckets)

    return {"buckets": buckets, "total": total}
