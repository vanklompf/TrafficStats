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


if __name__ == "__main__":
    unittest.main()
