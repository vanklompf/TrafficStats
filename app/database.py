import sqlite3
import threading
import os
import logging
from datetime import datetime, timedelta, timezone

from app.sun import get_no_collection_ranges, is_daytime

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
    """Insert a single car-passing event with the current UTC timestamp.
    Only inserts when the current time is between sunrise and sunset at the
    configured location (CITY). If not set, records 24/7.
    """
    now_utc = datetime.now(timezone.utc)
    if not is_daytime(now_utc):
        logger.debug("Skipping event outside collection window (sunrise–sunset)")
        return
    conn = _get_conn()
    now = now_utc.strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO events (timestamp, camera, direction) VALUES (?, ?, ?)",
        (now, camera, direction),
    )
    conn.commit()
    logger.info("Event recorded: camera=%s direction=%s at %s", camera, direction, now)


def get_stats(range_key: str) -> dict:
    """
    Return event counts aggregated into 5-minute buckets, split by direction.

    range_key: '24h' or 'week'
    Returns: {
        'buckets': [{'time': '...', 'count': N, 'left_to_right': N, 'right_to_left': N}, ...],
        'total': N,
        'total_left_to_right': N,
        'total_right_to_left': N,
        'peak_1min': N,
        'peak_1min_time': str or None,
        'peak_5min_time': str or None,
        'peak_1h': N,
        'peak_1h_time': str or None,
        'no_collection_ranges': [{'start': str, 'end': str}, ...],
    }
    """
    conn = _get_conn()

    if range_key == "week":
        since = datetime.now(timezone.utc) - timedelta(days=7)
    else:
        since = datetime.now(timezone.utc) - timedelta(hours=24)

    since_str = since.strftime("%Y-%m-%d %H:%M:%S")

    # Group into 5-minute buckets with per-direction counts
    rows = conn.execute(
        """
        SELECT
            strftime('%Y-%m-%d %H:', timestamp)
                || printf('%02d', (CAST(strftime('%M', timestamp) AS INTEGER) / 5) * 5)
                AS bucket,
            COUNT(*) AS count,
            SUM(CASE WHEN direction = 'LeftToRight' THEN 1 ELSE 0 END) AS left_to_right,
            SUM(CASE WHEN direction = 'RightToLeft' THEN 1 ELSE 0 END) AS right_to_left
        FROM events
        WHERE timestamp >= ?
        GROUP BY bucket
        ORDER BY bucket
        """,
        (since_str,),
    ).fetchall()

    buckets = [
        {
            "time": row["bucket"],
            "count": row["count"],
            "left_to_right": row["left_to_right"],
            "right_to_left": row["right_to_left"],
        }
        for row in rows
    ]
    total = sum(b["count"] for b in buckets)
    total_ltr = sum(b["left_to_right"] for b in buckets)
    total_rtl = sum(b["right_to_left"] for b in buckets)

    # -- Per-minute counts for sliding-window peaks ----------------------------
    minute_rows = conn.execute(
        """
        SELECT strftime('%Y-%m-%d %H:%M', timestamp) AS minute,
               COUNT(*) AS cnt
        FROM events
        WHERE timestamp >= ?
        GROUP BY minute
        ORDER BY minute
        """,
        (since_str,),
    ).fetchall()

    # 1-minute peak (unchanged logic, just reuse the query)
    if minute_rows:
        best_1min = max(minute_rows, key=lambda r: r["cnt"])
        peak_1min = best_1min["cnt"]
        peak_1min_time = best_1min["minute"]
    else:
        peak_1min = 0
        peak_1min_time = None

    # Build a continuous minute series for sliding-window computation
    peak_5min = 0
    peak_5min_time = None
    peak_1h = 0
    peak_1h_time = None

    if minute_rows:
        minute_counts = {r["minute"]: r["cnt"] for r in minute_rows}
        first = datetime.strptime(minute_rows[0]["minute"], "%Y-%m-%d %H:%M")
        last = datetime.strptime(minute_rows[-1]["minute"], "%Y-%m-%d %H:%M")

        # Generate continuous list of (minute_key, count) from first to last
        n_minutes = int((last - first).total_seconds() // 60) + 1
        minutes = []
        counts = []
        for i in range(n_minutes):
            key = (first + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M")
            minutes.append(key)
            counts.append(minute_counts.get(key, 0))

        # Sliding-window helper
        def _sliding_peak(window_size):
            if not counts:
                return 0, None
            if len(counts) <= window_size:
                return sum(counts), minutes[0]
            window_sum = sum(counts[:window_size])
            best_sum = window_sum
            best_idx = 0
            for i in range(1, len(counts) - window_size + 1):
                window_sum += counts[i + window_size - 1] - counts[i - 1]
                if window_sum > best_sum:
                    best_sum = window_sum
                    best_idx = i
            return best_sum, minutes[best_idx]

        peak_5min, peak_5min_time = _sliding_peak(5)
        peak_1h, peak_1h_time = _sliding_peak(60)

    # No-collection bands (sunset to sunrise) for the chart when location is set
    now_utc = datetime.now(timezone.utc)
    no_collection_ranges = get_no_collection_ranges(since, now_utc)

    return {
        "buckets": buckets,
        "total": total,
        "total_left_to_right": total_ltr,
        "total_right_to_left": total_rtl,
        "peak_1min": peak_1min,
        "peak_1min_time": peak_1min_time,
        "peak_5min": peak_5min,
        "peak_5min_time": peak_5min_time,
        "peak_1h": peak_1h,
        "peak_1h_time": peak_1h_time,
        "no_collection_ranges": no_collection_ranges,
    }
