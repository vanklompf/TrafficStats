---
name: Video analysis test harness
overview: Create a standalone test harness script that extracts frames from DAV/MP4 videos using multiple strategies, sends them to different Ollama vision models, and produces a structured comparison of quality, speed, and efficiency across all combinations.
todos:
  - id: create-script
    content: Create `tools/test_video_analysis.py` with CLI argument parsing (argparse), video discovery, and the main test loop
    status: completed
  - id: frame-extraction
    content: Implement four frame extraction strategies (interval, motion, keyframe, uniform); interval/keyframe/uniform via ffmpeg; motion via ffmpeg candidates + PIL pixel-diff filter
    status: completed
  - id: ollama-integration
    content: Implement Ollama API call with multi-image support, response parsing, and metric capture (tokens, duration)
    status: completed
  - id: results-output
    content: Implement JSON output and formatted console table (using tabulate or manual formatting)
    status: completed
  - id: video-discovery
    content: Implement auto-discovery of DAV files from media path with DAV-to-MP4 conversion in temp directory
    status: completed
isProject: false
---

# Video Analysis Test Harness

## Context

The app currently analyzes single JPG snapshots via Ollama (`app/analysis.py`). The goal is a standalone script to evaluate video-based analysis by comparing different models, frame extraction methods, frame counts, and resolutions -- using existing camera recordings as test data.

Videos live in `MEDIA_HOST_PATH` (`/mnt/nas/media/backup/kamery/kamera_front`) organized by date subdirectories (`YYYY-MM-DD/`), in Dahua `.dav` format. The app already has ffmpeg-based DAV-to-MP4 conversion in [app/intrusions.py](app/intrusions.py).

Ollama is available at `http://ollama:11434` (from Docker) or a user-specified host. The `/api/chat` endpoint accepts multiple base64 images in a single message.

## Script Location

`tools/test_video_analysis.py` -- standalone, no dependency on `app/` modules. Requires only `httpx`, `Pillow` (already in [requirements.txt](requirements.txt)), plus `ffmpeg` on PATH.

## Frame Extraction Strategies

The script supports four extraction methods:

- `**interval**` -- Extract one frame every N seconds via ffmpeg `-vf "fps=1/N"`. Configurable interval (defaults: 1, 2, 5 s). Simple and predictable.
- `**motion**` -- Pixel-level change detection (replaces the originally planned ffmpeg scene filter). Candidate frames are sampled at a configurable rate (default 0.5 s). Each candidate is compared to the *last kept* frame using PIL `ImageChops.difference`; if the normalised mean pixel difference (0–1) exceeds a threshold, the frame is kept. The first frame is always kept. Good for security cameras with long static periods; thresholds are small (e.g. 0.01, 0.02, 0.05). Implemented as: ffmpeg extracts candidates to a temp dir, then Python compares consecutive images and keeps only those above threshold.
- `**keyframe`** -- Extract only I-frames via ffmpeg `-skip_frame nokey` and `-vsync vfr`. Zero config; captures natural video structure.
- `**uniform*`* -- Extract exactly N frames evenly spaced across the video. Duration is probed with ffprobe; for each of N frames, ffmpeg seeks to `t` and outputs one frame (`-ss t -frames:v 1`), with a 0.5 s margin from the end. Useful to test fixed frame counts regardless of video length.

Each strategy outputs temporary JPEG files, which are then resized (if a max-width is set) and base64-encoded for the Ollama API.

## Dimensions to Compare

Each test run is a combination of:


| Dimension                           | CLI flag                    | Default                                       |
| ----------------------------------- | --------------------------- | --------------------------------------------- |
| Video file(s)                       | `--videos` / `--media-path` | auto-discover from media path                 |
| Models                              | `--models`                  | `qwen3-vl:8b`                                 |
| Extraction method                   | `--methods`                 | all four (`interval,motion,keyframe,uniform`) |
| Frame interval (for `interval`)     | `--intervals`               | `1,2,5`                                       |
| Motion threshold (for `motion`)     | `--motion-thresholds`       | `0.01,0.02,0.05` (0–1 pixel diff)             |
| Motion sample rate (for `motion`)   | `--motion-sample-rate`      | `0.5` (seconds between candidates)            |
| Uniform frame count (for `uniform`) | `--frame-counts`            | `3,5,10`                                      |
| Max image width (px)                | `--widths`                  | `512,768,1024`                                |
| Prompt                              | `--prompt`                  | security camera analysis prompt               |


## Ollama API Integration

Sends all extracted frames as a multi-image message to `POST {ollama_host}/api/chat`:

```python
{
    "model": model_name,
    "stream": False,
    "messages": [{
        "role": "user",
        "content": prompt,
        "images": [frame1_b64, frame2_b64, ...]
    }]
}
```

Captures from the response: `model`, `message.content`, `eval_count` (tokens), `total_duration`, `eval_duration`.

## Output

- **Console**: Rich-formatted table showing each run's key metrics (model, method, params, frame count, image data size, response time, token count, truncated response).
- **JSON file** (`--output`, default `test_results.json`): Full results with all metadata for post-analysis.
- Each result record:

```python
{
    "video": "2025-03-01/12.30.00-12.32.00[Intrusion][0@0][0].dav",
    "model": "qwen3-vl:8b",
    "method": "motion",
    "method_params": {"threshold": 0.02, "sample_rate": 0.5},
    "width": 768,
    "frames_extracted": 7,
    "total_image_bytes": 245000,
    "prompt": "...",
    "response": "A car is seen driving...",
    "response_tokens": 42,
    "duration_extract_s": 1.2,
    "duration_llm_s": 8.5,
    "duration_total_s": 9.7,
    "ollama_eval_count": 42,
    "ollama_total_duration_ns": 8500000000,
    "error": null
}
```

## CLI Usage Examples

```bash
# Auto-discover videos, test all methods with default model
python tools/test_video_analysis.py --media-path /mnt/nas/media/backup/kamery/kamera_front

# Specific video, compare two models
python tools/test_video_analysis.py \
  --videos /path/to/video.dav \
  --models qwen3-vl:8b,llava:13b \
  --methods interval,motion \
  --intervals 2,5

# Limit to 3 test videos, specific widths
python tools/test_video_analysis.py \
  --media-path /media \
  --max-videos 3 \
  --widths 512,1024 \
  --output results.json
```

## Video Discovery

When `--media-path` is given instead of explicit `--videos`, the script scans date subdirectories for `.dav` files, picks up to `--max-videos` (default 3) recent ones, and converts them to MP4 in a temp directory before frame extraction (reusing the ffmpeg approach from `app/intrusions.py`).

## Key Implementation Details

- Temporary files (extracted frames, converted MP4s) are cleaned up after each run using `tempfile.TemporaryDirectory`.
- Frame extraction calls ffmpeg as a subprocess (consistent with the existing app approach).
- Progress is printed to stderr so JSON output to stdout remains clean.
- The script handles DAV files by first converting to MP4, then extracting frames from the MP4.
- Errors (model not found, ffmpeg failure, timeout) are captured per-run, not fatal -- the harness continues with remaining combinations.
- A `--dry-run` flag shows the test matrix without calling Ollama.
- A `--ollama-host` flag (default `http://localhost:11434`) for running outside Docker.
- Optional `--num-ctx` and `--ollama-timeout` (default 600 s) for Ollama requests.

### Extraction implementation notes

- **Scene → motion**: The original plan specified ffmpeg’s `scene` filter for change detection. The implementation uses a **motion** method instead: ffmpeg dumps candidate frames at `sample_rate`, then Python (PIL `ImageChops.difference`) compares each candidate to the last kept frame and keeps it only if the normalised mean pixel difference exceeds `threshold`. This gives fine control and works well on static camera footage.
- **Uniform**: Implemented as N separate ffmpeg invocations (one seek + one frame each) rather than a single filter; duration comes from `ffprobe`.
- **Keyframe**: Uses `-vsync vfr` so output filenames match the sparse keyframe stream.

