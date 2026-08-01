import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("VIDEO_HW_ACCEL", "off")

from app import analysis, database


class AnalysisDatabaseTests(unittest.TestCase):
    def tearDown(self):
        database.close_conn()

    def _init_db(self, path: Path) -> None:
        database.close_conn()
        self.db_patch = patch.object(database, "DB_PATH", str(path))
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        database.init_db()

    def _insert_event(self, timestamp: str) -> int:
        conn = database._get_conn()
        cursor = conn.execute(
            "INSERT INTO events (timestamp, event_type) VALUES (?, 'intrusion')",
            (timestamp,),
        )
        conn.commit()
        return cursor.lastrowid

    def test_legacy_failures_migrate_to_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "traffic.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE events (id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, "
                "camera TEXT NOT NULL DEFAULT '', direction TEXT NOT NULL DEFAULT '', "
                "event_type TEXT NOT NULL DEFAULT 'intrusion', ivs_name TEXT NOT NULL DEFAULT '')"
            )
            conn.execute(
                "CREATE TABLE event_analysis (event_id INTEGER PRIMARY KEY, "
                "status TEXT NOT NULL, analysis TEXT, model TEXT, completed_at TEXT)"
            )
            conn.execute("INSERT INTO events (id, timestamp) VALUES (1, '2026-08-02 10:00:00')")
            conn.execute("INSERT INTO event_analysis (event_id, status) VALUES (1, 'failed')")
            conn.commit()
            conn.close()

            self._init_db(db_path)

            self.assertEqual(database.get_analysis(1)["status"], "terminal_failure")
            self.assertEqual(database.get_intrusion_analysis_backfill(), [])

    def test_backfill_is_newest_first_and_only_retries_retryable_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._init_db(Path(tmp) / "traffic.db")
            event_ids = [
                self._insert_event(f"2026-08-02 10:0{i}:00") for i in range(5)
            ]
            database.update_analysis(event_ids[0], "terminal_failure")
            database.update_analysis(event_ids[1], "done", analysis="ok")
            database.mark_analysis_processing(event_ids[2])
            database.schedule_analysis_retry(
                event_ids[3], "retryable_failure", "ConnectError", 60
            )

            jobs = database.get_intrusion_analysis_backfill()

            self.assertEqual(
                [job["id"] for job in jobs],
                [event_ids[4], event_ids[3], event_ids[2]],
            )
            self.assertEqual(jobs[1]["status"], "retryable_failure")
            self.assertIsNotNone(jobs[1]["next_retry_at"])


class AnalysisWorkerQueueTests(unittest.TestCase):
    def test_live_events_precede_retries_and_backfill(self):
        worker = analysis.AnalysisWorker()
        processed = []

        with patch.object(analysis, "get_event_by_id", return_value=None):
            worker._enqueue(1, priority=2, source="backfill")
            worker._enqueue(2, priority=1, source="retry")
            worker._enqueue(3, priority=0, source="live")

        def process(event_id):
            processed.append(event_id)
            if len(processed) == 3:
                worker._stop.set()

        with patch.object(worker, "_process_one", side_effect=process):
            thread = threading.Thread(target=worker._run)
            thread.start()
            thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(processed, [3, 2, 1])

    def test_delayed_retry_does_not_block_ready_backfill(self):
        worker = analysis.AnalysisWorker()
        processed = []

        with patch.object(analysis, "get_event_by_id", return_value=None):
            worker._enqueue(1, priority=1, source="retry", delay_seconds=60)
            worker._enqueue(2, priority=2, source="backfill")

        def process(event_id):
            processed.append(event_id)
            worker._stop.set()

        with patch.object(worker, "_process_one", side_effect=process):
            thread = threading.Thread(target=worker._run)
            thread.start()
            thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(processed, [2])


class MediaPendingTests(unittest.TestCase):
    def tearDown(self):
        database.close_conn()

    def test_missing_recent_media_is_scheduled_without_polling(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "traffic.db"
            database.close_conn()
            with patch.object(database, "DB_PATH", str(db_path)):
                database.init_db()
                timestamp = (datetime.now(timezone.utc) - timedelta(seconds=5)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                conn = database._get_conn()
                cursor = conn.execute(
                    "INSERT INTO events (timestamp, event_type) VALUES (?, 'intrusion')",
                    (timestamp,),
                )
                conn.commit()
                event_id = cursor.lastrowid
                worker = analysis.AnalysisWorker()
                worker._processing.add(event_id)

                with patch.object(analysis, "match_media_for_events", return_value=[]):
                    worker._process_one(event_id)

                result = database.get_analysis(event_id)

            self.assertEqual(result["status"], "media_pending")
            self.assertEqual(result["failure_reason"], "media_not_ready")
            self.assertEqual(worker.get_queue_size(), 1)
            self.assertEqual(worker.get_queue_contents()[0]["source"], "retry")


if __name__ == "__main__":
    unittest.main()
