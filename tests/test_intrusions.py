import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("VIDEO_HW_ACCEL", "off")

from app import intrusions
from app.intrusions import get_file_fingerprint, wait_for_stable_file


class WaitForStableFileTests(unittest.TestCase):
    def test_returns_fingerprint_after_file_stays_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recording.dav"
            path.write_bytes(b"initial data")

            result = wait_for_stable_file(
                path,
                timeout=0.5,
                stable_seconds=0.08,
                poll_seconds=0.02,
            )

            self.assertEqual(result, get_file_fingerprint(path))

    def test_times_out_while_file_keeps_growing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recording.dav"
            path.write_bytes(b"initial data")
            stop = threading.Event()

            def grow_file():
                while not stop.wait(0.02):
                    with path.open("ab") as output:
                        output.write(b"more data")

            writer = threading.Thread(target=grow_file)
            writer.start()
            try:
                result = wait_for_stable_file(
                    path,
                    timeout=0.2,
                    stable_seconds=0.08,
                    poll_seconds=0.01,
                )
            finally:
                stop.set()
                writer.join()

            self.assertIsNone(result)

    def test_stop_event_interrupts_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recording.dav"
            path.write_bytes(b"initial data")
            stop = threading.Event()
            stop.set()

            started = time.monotonic()
            result = wait_for_stable_file(
                path,
                timeout=10,
                stable_seconds=1,
                poll_seconds=1,
                stop_event=stop,
            )

            self.assertIsNone(result)
            self.assertLess(time.monotonic() - started, 0.1)


class CachedVideoFingerprintTests(unittest.TestCase):
    def test_cache_is_invalidated_when_source_grows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media_root = root / "media"
            cache_root = root / "cache"
            date = "2026-08-02"
            source_dir = media_root / date
            cached_dir = cache_root / date
            source_dir.mkdir(parents=True)
            cached_dir.mkdir(parents=True)
            source = source_dir / "10.00.00-10.01.00[M].dav"
            cached = cached_dir / "10.00.00-10.01.00[M].mp4"
            source.write_bytes(b"initial data")
            cached.write_bytes(b"converted data")
            fingerprint = get_file_fingerprint(source)
            self.assertIsNotNone(fingerprint)
            self.assertTrue(intrusions._write_cached_fingerprint(cached, fingerprint))

            with patch.object(intrusions, "MEDIA_PATH", str(media_root)), patch.object(
                intrusions, "VIDEO_CACHE_DIR", str(cache_root)
            ):
                self.assertEqual(
                    intrusions.get_cached_video_path(date, source.name), cached
                )
                with source.open("ab") as output:
                    output.write(b"more data")
                self.assertIsNone(
                    intrusions.get_cached_video_path(date, source.name)
                )


class SafeMediaPathTests(unittest.TestCase):
    def test_regular_media_file_resolves_under_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            date_dir = root / "2026-08-02"
            date_dir.mkdir(parents=True)
            media = date_dir / "recording.dav"
            media.write_bytes(b"video")

            with patch.object(intrusions, "MEDIA_PATH", str(root)):
                self.assertEqual(
                    intrusions.get_media_path("2026-08-02", media.name),
                    media.resolve(),
                )

    def test_symlinked_media_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "media"
            date_dir = root / "2026-08-02"
            date_dir.mkdir(parents=True)
            secret = base / "secret"
            secret.write_text("SESSION_SECRET=secret", encoding="ascii")
            (date_dir / "recording.dav").symlink_to(secret)

            with patch.object(intrusions, "MEDIA_PATH", str(root)):
                self.assertIsNone(
                    intrusions.get_media_path("2026-08-02", "recording.dav")
                )

    def test_symlinked_date_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "media"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            (outside / "recording.dav").write_bytes(b"secret")
            (root / "2026-08-02").symlink_to(outside, target_is_directory=True)

            with patch.object(intrusions, "MEDIA_PATH", str(root)):
                self.assertIsNone(intrusions.get_media_path("2026-08-02"))
                self.assertIsNone(
                    intrusions.get_media_path("2026-08-02", "recording.dav")
                )

    def test_path_components_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            root.mkdir()

            with patch.object(intrusions, "MEDIA_PATH", str(root)):
                self.assertIsNone(intrusions.get_media_path("../outside", "file"))
                self.assertIsNone(
                    intrusions.get_media_path("2026-08-02", "../secret")
                )

    def test_fingerprint_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            link = root / "link"
            target.write_bytes(b"secret")
            link.symlink_to(target)

            self.assertIsNone(intrusions.get_file_fingerprint(link))


class RecordingSelectionTests(unittest.TestCase):
    DATE = "2026-08-02"

    def _match(self, filenames, event_time):
        with tempfile.TemporaryDirectory() as tmp:
            media_root = Path(tmp)
            date_dir = media_root / self.DATE
            date_dir.mkdir()
            for filename in filenames:
                (date_dir / filename).touch()

            event = {"id": 1, "timestamp": f"{self.DATE} {event_time}"}
            with patch.object(intrusions, "MEDIA_PATH", str(media_root)), patch.object(
                intrusions, "_local_tz", intrusions.ZoneInfo("UTC")
            ):
                matched = intrusions.match_media_for_events([event], self.DATE)[0]
                event_utc = intrusions.datetime.strptime(
                    event["timestamp"], "%Y-%m-%d %H:%M:%S"
                )
                recording_end = intrusions.get_recording_end_utc(event_utc)
            return matched, recording_end

    def test_exact_containment_beats_earlier_tolerance_match(self):
        earlier = "09.59.00-10.00.00[M].dav"
        containing = "10.00.00-10.01.00[M].dav"

        matched, recording_end = self._match(
            [earlier, containing], "10:00:20"
        )

        self.assertEqual(matched["video"], containing)
        self.assertEqual(recording_end.strftime("%H:%M:%S"), "10:01:00")

    def test_closest_start_wins_when_recordings_overlap(self):
        earlier = "09.59.00-10.01.00[M].dav"
        later = "10.00.10-10.01.10[M].dav"

        matched, _ = self._match([earlier, later], "10:00:20")

        self.assertEqual(matched["video"], later)

    def test_nearest_boundary_wins_for_tolerance_only_matches(self):
        earlier = "09.59.00-10.00.00[M].dav"
        later = "10.00.30-10.01.00[M].dav"

        matched, _ = self._match([earlier, later], "10:00:20")

        self.assertEqual(matched["video"], later)

    def test_filesystem_order_does_not_change_selection(self):
        earlier = "09.59.00-10.00.00[M].dav"
        containing = "10.00.00-10.01.00[M].dav"

        first, _ = self._match([earlier, containing], "10:00:20")
        second, _ = self._match([containing, earlier], "10:00:20")

        self.assertEqual(first["video"], containing)
        self.assertEqual(second["video"], containing)

    def test_media_scan_uses_camera_local_event_date(self):
        event = {"id": 1, "timestamp": "2026-08-02 23:55:00"}
        local_date = intrusions.datetime(2026, 8, 3).date()

        with patch.object(
            intrusions, "_local_tz", intrusions.ZoneInfo("Asia/Tokyo")
        ), patch.object(intrusions, "_scan_media", return_value=([], [])) as scan:
            intrusions.match_media_for_events([event], "2026-08-02")

        scan.assert_called_once_with(local_date)


if __name__ == "__main__":
    unittest.main()
