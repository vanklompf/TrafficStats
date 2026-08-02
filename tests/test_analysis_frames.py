import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app import analysis


class MotionFrameSelectionTests(unittest.TestCase):
    def test_preserves_endpoints_and_strongest_frame_per_time_segment(self):
        frames = [
            (Path(f"frame_{i}.jpg"), float(i), score)
            for i, score in enumerate([0.0, 0.1, 0.2, 0.9, 0.1, 0.2, 0.8, 0.1, 0.2, 0.1])
        ]

        selected = analysis._select_motion_frames(frames, 4)

        self.assertEqual([frame[0].name for frame in selected], [
            "frame_0.jpg", "frame_3.jpg", "frame_6.jpg", "frame_9.jpg",
        ])

    def test_fills_empty_time_segments_with_well_spaced_frames(self):
        frames = [
            (Path(f"frame_{i}.jpg"), offset, 0.1)
            for i, offset in enumerate([0.0, 1.0, 2.0, 98.0, 99.0, 100.0])
        ]

        selected = analysis._select_motion_frames(frames, 5)

        self.assertEqual(len(selected), 5)
        self.assertEqual(selected[0], frames[0])
        self.assertEqual(selected[-1], frames[-1])
        self.assertEqual(selected, sorted(selected, key=lambda frame: frame[1]))

    def test_motion_extraction_caps_and_renames_selected_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "frames"
            out_dir.mkdir()

            def create_candidates(cmd, timeout=120):
                pattern = Path(cmd[-1])
                for i in range(10):
                    image = Image.new("L", (8, 8), color=i * 20)
                    image.save(pattern.parent / f"cand_{i + 1:06d}.jpg")
                return True

            with patch.object(analysis, "_run_ffmpeg", side_effect=create_candidates):
                frames, span = analysis._extract_frames_motion(
                    Path("video.mp4"), out_dir,
                    threshold=0.01,
                    sample_rate=1.0,
                    max_frames=4,
                )

            self.assertEqual([path.name for path in frames], [
                "frame_0000.jpg", "frame_0001.jpg", "frame_0002.jpg", "frame_0003.jpg",
            ])
            self.assertEqual(span, 9.0)
            self.assertFalse((out_dir / "_candidates").exists())


if __name__ == "__main__":
    unittest.main()
