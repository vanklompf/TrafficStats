import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("VIDEO_HW_ACCEL", "off")

from app import database, intrusions
from app.dahua import DahuaListener


class DahuaEventIdentityTests(unittest.TestCase):
    def test_listener_preserves_camera_identity(self):
        listener = DahuaListener(
            host="camera-1",
            user="user",
            password="password",
            intrusion_ivs_name="yard",
        )
        line = (
            'Code=CrossLineDetection;action=Start;index=2;data={'
            '"Name":"yard","Direction":"LeftToRight","EventID":731,'
            '"Sequence":88,"UTC":"2026-08-02T10:00:20Z","Channel":2}'
        )

        with patch("app.dahua.insert_event", return_value=10) as insert:
            listener._process_line(line)

        insert.assert_called_once_with(
            camera="camera-1",
            direction="LeftToRight",
            event_type="intrusion",
            ivs_name="yard",
            source_event_id="731",
            source_sequence="88",
            source_timestamp="2026-08-02 10:00:20",
            channel="2",
        )

    def test_naive_event_time_uses_camera_timezone(self):
        listener = DahuaListener(
            host="camera-1",
            user="user",
            password="password",
            intrusion_ivs_name="yard",
        )
        line = (
            'Code=CrossLineDetection;action=Start;index=2;data={'
            '"Name":"yard","EventTime":"2026-08-02 12:00:20"}'
        )

        with patch.dict(os.environ, {"TZ": "Europe/Amsterdam"}), patch(
            "app.dahua.insert_event", return_value=10
        ) as insert:
            listener._process_line(line)

        self.assertEqual(
            insert.call_args.kwargs["source_timestamp"], "2026-08-02 10:00:20"
        )


class EventIdentityDatabaseTests(unittest.TestCase):
    def tearDown(self):
        database.close_conn()

    def test_init_migrates_and_identity_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "traffic.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE events (id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, "
                "camera TEXT NOT NULL DEFAULT '', direction TEXT NOT NULL DEFAULT '', "
                "event_type TEXT NOT NULL DEFAULT 'traffic', ivs_name TEXT NOT NULL DEFAULT '')"
            )
            conn.commit()
            conn.close()

            with patch.object(database, "DB_PATH", str(db_path)):
                database.init_db()
                event_id = database.insert_event(
                    camera="camera-1",
                    event_type="intrusion",
                    ivs_name="yard",
                    source_event_id="731",
                    source_sequence="88",
                    source_timestamp="2026-08-02 10:00:20",
                    channel="2",
                )
                event = database.get_event_by_id(event_id)

            self.assertEqual(event["source_event_id"], "731")
            self.assertEqual(event["source_sequence"], "88")
            self.assertEqual(event["source_timestamp"], "2026-08-02 10:00:20")
            self.assertEqual(event["channel"], "2")


class MediaIdentityTests(unittest.TestCase):
    DATE = "2026-08-02"

    def test_source_time_and_channel_take_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            media_root = Path(tmp)
            date_dir = media_root / self.DATE
            date_dir.mkdir()
            wrong_channel = "10.00.10-10.00.40[M][1@0][0].dav"
            right_channel = "10.00.00-10.01.00[M][2@0][0].dav"
            (date_dir / wrong_channel).touch()
            (date_dir / right_channel).touch()
            event = {
                "id": 1,
                "timestamp": f"{self.DATE} 10:05:00",
                "source_timestamp": f"{self.DATE} 10:00:20",
                "channel": "2",
            }

            with patch.object(intrusions, "MEDIA_PATH", str(media_root)), patch.object(
                intrusions, "_local_tz", intrusions.ZoneInfo("UTC")
            ):
                matched = intrusions.match_media_for_events([event], self.DATE)[0]

            self.assertEqual(matched["video"], right_channel)

    def test_known_channel_beats_unknown_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            media_root = Path(tmp)
            date_dir = media_root / self.DATE
            date_dir.mkdir()
            unknown = "10.00.10-10.00.40[M].dav"
            exact = "10.00.00-10.01.00[M][2@0][0].dav"
            (date_dir / unknown).touch()
            (date_dir / exact).touch()
            event = {
                "id": 1,
                "timestamp": f"{self.DATE} 10:00:20",
                "channel": "2",
            }

            with patch.object(intrusions, "MEDIA_PATH", str(media_root)), patch.object(
                intrusions, "_local_tz", intrusions.ZoneInfo("UTC")
            ):
                matched = intrusions.match_media_for_events([event], self.DATE)[0]

            self.assertEqual(matched["video"], exact)

    def test_mixed_source_dates_are_scanned_separately(self):
        events = [
            {
                "id": 1,
                "timestamp": "2026-08-02 12:00:00",
                "source_timestamp": "2026-07-30 12:00:00",
            },
            {
                "id": 2,
                "timestamp": "2026-08-02 12:01:00",
                "source_timestamp": "2026-08-02 12:01:00",
            },
        ]

        with patch.object(intrusions, "_local_tz", intrusions.ZoneInfo("UTC")), patch.object(
            intrusions, "_scan_media", return_value=([], [])
        ) as scan:
            intrusions.match_media_for_events(events, self.DATE)

        self.assertEqual(
            [call.args[0].isoformat() for call in scan.call_args_list],
            ["2026-07-30", "2026-08-02"],
        )


if __name__ == "__main__":
    unittest.main()
