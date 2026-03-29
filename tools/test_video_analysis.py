#!/usr/bin/env python3
"""
Test harness for comparing video analysis approaches with Ollama vision models.

Extracts frames from DAV/MP4 videos using multiple strategies, sends them to
Ollama, and produces a structured comparison of quality, speed, and efficiency.

Uses existing camera recordings from the TrafficStats media directory as
test samples.

Requirements: httpx, Pillow, numpy, ffmpeg on PATH.
"""

import argparse
import base64
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# DAV file discovery
# ---------------------------------------------------------------------------

_DAV_RE = re.compile(
    r"^(\d{2})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2})\[.*\].*\.dav$",
    re.IGNORECASE,
)


def discover_videos(media_path: str, max_videos: int) -> list[Path]:
    """Find DAV/MP4 files in *media_path*.

    Looks in date-organised subdirectories (YYYY-MM-DD/) first, then falls
    back to files directly in the given directory.  Returns newest first,
    limited to *max_videos*.
    """
    root = Path(media_path)
    if not root.is_dir():
        _err(f"Media path does not exist: {root}")
        return []

    VIDEO_RE = re.compile(r".*\.(dav|mp4)$", re.IGNORECASE)

    found: list[Path] = []

    # 1) Date-organised subdirectories (camera FTP layout)
    for date_dir in sorted(root.iterdir(), reverse=True):
        if not date_dir.is_dir() or not re.match(r"\d{4}-\d{2}-\d{2}$", date_dir.name):
            continue
        for f in sorted(date_dir.iterdir(), reverse=True):
            if VIDEO_RE.match(f.name):
                found.append(f)
            if len(found) >= max_videos:
                break
        if len(found) >= max_videos:
            break

    # 2) Flat directory (files placed directly in the path)
    if not found:
        for f in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.is_file() and VIDEO_RE.match(f.name):
                found.append(f)
            if len(found) >= max_videos:
                break

    return found


def convert_dav_to_mp4(dav_path: Path, output_dir: Path) -> Path | None:
    """Convert a DAV file to MP4 using ffmpeg. Returns MP4 path or None."""
    mp4_name = dav_path.stem + ".mp4"
    mp4_path = output_dir / mp4_name
    if mp4_path.is_file():
        return mp4_path

    cmd = [
        "ffmpeg", "-y", "-i", str(dav_path),
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        "-movflags", "+faststart",
        str(mp4_path),
    ]
    _info(f"Converting {dav_path.name} -> MP4 ...")
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
        return mp4_path
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        _err(f"ffmpeg conversion failed for {dav_path.name}: {e}")
        mp4_path.unlink(missing_ok=True)
        return None


def get_video_duration(video_path: Path) -> float | None:
    """Probe video duration in seconds using ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return float(result.stdout.strip())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Hardware acceleration detection
# ---------------------------------------------------------------------------

_VAAPI_DEVICE = "/dev/dri/renderD128"


def _detect_hwaccel() -> str | None:
    """Probe for VAAPI hardware-accelerated decoding."""
    if not Path(_VAAPI_DEVICE).exists():
        return None
    try:
        subprocess.run(
            [
                "ffmpeg", "-v", "error",
                "-hwaccel", "vaapi", "-hwaccel_device", _VAAPI_DEVICE,
                "-f", "lavfi", "-i", "color=black:s=64x64:d=0.1",
                "-frames:v", "1", "-f", "null", "-",
            ],
            check=True, capture_output=True, timeout=10,
        )
        return "vaapi"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _hwaccel_input_flags(hwaccel: str | None) -> list[str]:
    """Return ffmpeg input flags for the given hwaccel mode."""
    if hwaccel == "vaapi":
        return ["-hwaccel", "vaapi", "-hwaccel_device", _VAAPI_DEVICE]
    return []


# ---------------------------------------------------------------------------
# Frame extraction strategies
# ---------------------------------------------------------------------------


def extract_frames_interval(
    video: Path, out_dir: Path, interval: float, *,
    hwaccel: str | None = None, width: int | None = None,
) -> list[Path]:
    """Extract one frame every *interval* seconds, optionally scaled to *width*."""
    pattern = str(out_dir / "frame_%04d.jpg")
    vf = f"scale={width}:-2,fps=1/{interval}" if width else f"fps=1/{interval}"
    cmd = [
        "ffmpeg", *_hwaccel_input_flags(hwaccel),
        "-i", str(video),
        "-vf", vf,
        "-q:v", "2",
        pattern,
    ]
    _run_ffmpeg(cmd)
    return sorted(out_dir.glob("frame_*.jpg"))


def _compute_frame_diff(
    img1: Image.Image,
    img2: Image.Image,
    mask_bool: np.ndarray | None = None,
) -> float:
    """Return normalised mean pixel difference (0.0 – 1.0) between two images.

    If *mask_bool* is provided (boolean ndarray, True=monitor, False=ignore),
    only True pixels contribute to the mean.  Must match the frame dimensions.
    """
    from PIL import ImageChops

    g1 = img1.convert("L")
    g2 = img2.convert("L")
    if g1.size != g2.size:
        g2 = g2.resize(g1.size, Image.LANCZOS)

    diff = ImageChops.difference(g1, g2)

    if mask_bool is not None:
        diff_arr = np.asarray(diff, dtype=np.float32)
        if diff_arr.shape != mask_bool.shape:
            raise ValueError(
                f"Mask size {mask_bool.shape[1]}x{mask_bool.shape[0]} "
                f"does not match frame size {g1.size[0]}x{g1.size[1]}"
            )
        masked = diff_arr[mask_bool]
        if masked.size == 0:
            return 0.0
        return float(masked.mean() / 255.0)

    hist = diff.histogram()
    total_pixels = g1.size[0] * g1.size[1]
    mean_diff = sum(i * count for i, count in enumerate(hist)) / total_pixels
    return mean_diff / 255.0


def extract_frames_motion(
    video: Path,
    out_dir: Path,
    threshold: float,
    sample_rate: float = 0.5,
    *,
    hwaccel: str | None = None,
    mask_bool: np.ndarray | None = None,
    width: int | None = None,
    cleanup_candidates: bool = False,
    timing_out: dict | None = None,
) -> list[Path]:
    """Extract frames where pixel-level change exceeds *threshold* (0-1).

    Candidate frames are sampled every *sample_rate* seconds.  Each candidate
    is compared to the last *kept* frame; if the average pixel difference
    (normalised to 0-1) exceeds *threshold*, the candidate is kept.  The first
    frame is always kept.

    If *width* is set, candidates are scaled to that width.  If *mask_bool* is
    provided (boolean ndarray, True=monitor, False=ignore), only True pixels
    contribute to the diff.  The mask must match the frame dimensions exactly;
    a mismatch raises ``ValueError``.

    If *cleanup_candidates* is True, the ``_candidates`` directory (including
    frames rejected by the motion threshold) is removed after processing so only
    ``frame_*.jpg`` remain under *out_dir*.
    """
    candidates_dir = out_dir / "_candidates"
    candidates_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(candidates_dir / "cand_%06d.jpg")
    vf = f"scale={width}:-2,fps=1/{sample_rate}" if width else f"fps=1/{sample_rate}"
    cmd = [
        "ffmpeg", *_hwaccel_input_flags(hwaccel),
        "-i", str(video),
        "-vf", vf,
        "-q:v", "2",
        pattern,
    ]
    t_ffmpeg0 = time.monotonic()
    _run_ffmpeg(cmd)
    t_ffmpeg1 = time.monotonic()

    candidate_paths = sorted(candidates_dir.glob("cand_*.jpg"))
    if not candidate_paths:
        if cleanup_candidates and candidates_dir.is_dir():
            shutil.rmtree(candidates_dir)
        return []

    kept: list[Path] = []
    ref_img: Image.Image | None = None
    frame_idx = 0

    t_motion0 = time.monotonic()
    for cp in candidate_paths:
        try:
            img = Image.open(cp)
            img.load()
        except Exception as e:
            _err(f"Cannot open candidate frame {cp.name}: {e}")
            continue

        if ref_img is None:
            dst = out_dir / f"frame_{frame_idx:04d}.jpg"
            cp.rename(dst)
            kept.append(dst)
            ref_img = img
            frame_idx += 1
            continue

        diff = _compute_frame_diff(ref_img, img, mask_bool=mask_bool)
        if diff >= threshold:
            dst = out_dir / f"frame_{frame_idx:04d}.jpg"
            cp.rename(dst)
            kept.append(dst)
            ref_img = img
            frame_idx += 1

    t_motion1 = time.monotonic()

    mask_label = " +mask" if mask_bool is not None else ""
    width_label = f" @{width}px" if width else ""
    _info(
        f"  Motion filter: {len(candidate_paths)} candidates -> "
        f"{len(kept)} kept (threshold={threshold}, sample_rate={sample_rate}s{width_label}{mask_label})"
    )

    t_clean0 = time.monotonic()
    if cleanup_candidates and candidates_dir.is_dir():
        shutil.rmtree(candidates_dir)
    t_clean1 = time.monotonic()

    if timing_out is not None:
        timing_out["ffmpeg_candidates_s"] = round(t_ffmpeg1 - t_ffmpeg0, 4)
        timing_out["motion_filter_s"] = round(t_motion1 - t_motion0, 4)
        timing_out["cleanup_candidates_s"] = round(t_clean1 - t_clean0, 4)
        timing_out["candidates_n"] = len(candidate_paths)
        timing_out["kept_n"] = len(kept)

    return kept


def extract_frames_keyframe(
    video: Path, out_dir: Path, *,
    hwaccel: str | None = None, width: int | None = None,
) -> list[Path]:
    """Extract only I-frames (keyframes), optionally scaled to *width*."""
    pattern = str(out_dir / "frame_%04d.jpg")
    vf_parts = []
    if width:
        vf_parts.append(f"scale={width}:-2")
    cmd = [
        "ffmpeg", *_hwaccel_input_flags(hwaccel),
        "-skip_frame", "nokey",
        "-i", str(video),
        "-vsync", "vfr",
        *((["-vf", ",".join(vf_parts)]) if vf_parts else []),
        "-q:v", "2",
        pattern,
    ]
    _run_ffmpeg(cmd)
    return sorted(out_dir.glob("frame_*.jpg"))


def extract_frames_uniform(
    video: Path, out_dir: Path, count: int, *,
    hwaccel: str | None = None, width: int | None = None,
) -> list[Path]:
    """Extract exactly *count* frames evenly spaced, optionally scaled to *width*."""
    duration = get_video_duration(video)
    if duration is None or duration <= 0:
        _err(f"Cannot determine duration for {video.name}")
        return []

    usable = max(duration - 0.5, 0.1)
    vf_args = ["-vf", f"scale={width}:-2"] if width else []
    frames: list[Path] = []
    for i in range(count):
        t = (usable * i) / max(count - 1, 1) if count > 1 else usable / 2
        out_path = out_dir / f"frame_{i:04d}.jpg"
        cmd = [
            "ffmpeg", *_hwaccel_input_flags(hwaccel),
            "-ss", f"{t:.3f}",
            "-i", str(video),
            *vf_args,
            "-frames:v", "1", "-q:v", "2",
            str(out_path),
        ]
        _run_ffmpeg(cmd)
        if out_path.is_file():
            frames.append(out_path)
    return frames


def _run_ffmpeg(cmd: list[str]) -> None:
    try:
        subprocess.run(
            cmd, check=True, capture_output=True, timeout=120,
        )
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"")[-500:].decode("utf-8", errors="replace")
        _err(f"ffmpeg error: {stderr}")
    except subprocess.TimeoutExpired:
        _err("ffmpeg timed out")


# ---------------------------------------------------------------------------
# Image processing
# ---------------------------------------------------------------------------


def load_and_encode_frames(frame_paths: list[Path]) -> tuple[list[str], int]:
    """Load already-scaled frames, JPEG-encode, and return (base64 list, total bytes)."""
    encoded: list[str] = []
    total_bytes = 0

    for fp in frame_paths:
        try:
            with Image.open(fp) as img:
                img.load()
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")

                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=85, optimize=True)
                data = buf.getvalue()
                total_bytes += len(data)
                encoded.append(base64.b64encode(data).decode("ascii"))
        except Exception as e:
            _err(f"Failed to process frame {fp.name}: {e}")

    return encoded, total_bytes


# ---------------------------------------------------------------------------
# Ollama API
# ---------------------------------------------------------------------------


def call_ollama(
    host: str,
    model: str,
    prompt: str,
    images_b64: list[str],
    timeout: float,
    num_ctx: int | None = None,
) -> dict:
    """Send images to Ollama and return parsed result dict."""
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": images_b64,
            }
        ],
    }
    if num_ctx is not None:
        payload["options"] = {"num_ctx": num_ctx}

    url = f"{host.rstrip('/')}/api/chat"
    t0 = time.monotonic()
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return {
            "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}",
            "duration_llm_s": time.monotonic() - t0,
        }
    except Exception as e:
        return {
            "error": str(e),
            "duration_llm_s": time.monotonic() - t0,
        }

    elapsed = time.monotonic() - t0
    message = data.get("message") or {}
    return {
        "response": (message.get("content") or "").strip(),
        "model_used": data.get("model") or model,
        "duration_llm_s": elapsed,
        "ollama_total_duration_ns": data.get("total_duration"),
        "ollama_eval_duration_ns": data.get("eval_duration"),
        "ollama_eval_count": data.get("eval_count"),
        "ollama_prompt_eval_count": data.get("prompt_eval_count"),
        "error": None,
    }


# ---------------------------------------------------------------------------
# Console output helpers
# ---------------------------------------------------------------------------

def _info(msg: str) -> None:
    print(f"  {msg}", file=sys.stderr)


def _err(msg: str) -> None:
    print(f"  ERROR: {msg}", file=sys.stderr)


def _header(msg: str) -> None:
    print(f"\n{'=' * 60}", file=sys.stderr)
    print(f"  {msg}", file=sys.stderr)
    print(f"{'=' * 60}", file=sys.stderr)


def print_results_table(results: list[dict]) -> None:
    """Print a formatted comparison table to stderr."""
    if not results:
        return

    _header("RESULTS")

    col_w = {
        "video": 30,
        "model": 16,
        "method": 18,
        "width": 5,
        "frames": 6,
        "img_kb": 7,
        "llm_s": 6,
        "tokens": 6,
        "response": 40,
    }

    header = (
        f"{'Video':<{col_w['video']}} "
        f"{'Model':<{col_w['model']}} "
        f"{'Method':<{col_w['method']}} "
        f"{'Width':>{col_w['width']}} "
        f"{'Frms':>{col_w['frames']}} "
        f"{'ImgKB':>{col_w['img_kb']}} "
        f"{'LLMs':>{col_w['llm_s']}} "
        f"{'Toks':>{col_w['tokens']}} "
        f"{'Response':<{col_w['response']}}"
    )
    print(f"\n{header}", file=sys.stderr)
    print("-" * len(header), file=sys.stderr)

    for r in results:
        video_short = r.get("video", "?")
        if len(video_short) > col_w["video"]:
            video_short = "..." + video_short[-(col_w["video"] - 3):]

        resp = r.get("response") or r.get("error") or ""
        if len(resp) > col_w["response"]:
            resp = resp[:col_w["response"] - 3] + "..."

        img_kb = (r.get("total_image_bytes") or 0) / 1024
        llm_s = r.get("duration_llm_s") or 0
        tokens = r.get("ollama_eval_count") or 0
        method_str = r.get("method", "?")
        params = r.get("method_params") or {}
        if params:
            param_val = list(params.values())[0]
            method_str = f"{method_str}({param_val})"

        line = (
            f"{video_short:<{col_w['video']}} "
            f"{r.get('model', '?'):<{col_w['model']}} "
            f"{method_str:<{col_w['method']}} "
            f"{r.get('width', '?'):>{col_w['width']}} "
            f"{r.get('frames_extracted', 0):>{col_w['frames']}} "
            f"{img_kb:>{col_w['img_kb']}.0f} "
            f"{llm_s:>{col_w['llm_s']}.1f} "
            f"{tokens:>{col_w['tokens']}} "
            f"{resp:<{col_w['response']}}"
        )
        print(line, file=sys.stderr)

    print(file=sys.stderr)


# ---------------------------------------------------------------------------
# Test matrix generation
# ---------------------------------------------------------------------------


def build_test_matrix(args: argparse.Namespace) -> list[dict]:
    """Build list of test configurations from CLI arguments."""
    matrix: list[dict] = []

    if args.extract_only:
        # In extract-only mode, skip model dimension (no LLM); still honor --widths
        # so ffmpeg scaling and motion masks match frame size.
        for width in args.widths:
            for method in args.methods:
                if method == "interval":
                    for interval in args.intervals:
                        matrix.append({
                            "model": "extract-only",
                            "width": width,
                            "method": "interval",
                            "method_params": {"interval": interval},
                        })
                elif method == "motion":
                    for threshold in args.motion_thresholds:
                        matrix.append({
                            "model": "extract-only",
                            "width": width,
                            "method": "motion",
                            "method_params": {
                                "threshold": threshold,
                                "sample_rate": args.motion_sample_rate,
                            },
                        })
                elif method == "keyframe":
                    matrix.append({
                        "model": "extract-only",
                        "width": width,
                        "method": "keyframe",
                        "method_params": {},
                    })
                elif method == "uniform":
                    for count in args.frame_counts:
                        matrix.append({
                            "model": "extract-only",
                            "width": width,
                            "method": "uniform",
                            "method_params": {"count": count},
                        })
        return matrix

    for model in args.models:
        for width in args.widths:
            for method in args.methods:
                if method == "interval":
                    for interval in args.intervals:
                        matrix.append({
                            "model": model,
                            "width": width,
                            "method": "interval",
                            "method_params": {"interval": interval},
                        })
                elif method == "motion":
                    for threshold in args.motion_thresholds:
                        matrix.append({
                            "model": model,
                            "width": width,
                            "method": "motion",
                            "method_params": {
                                "threshold": threshold,
                                "sample_rate": args.motion_sample_rate,
                            },
                        })
                elif method == "keyframe":
                    matrix.append({
                        "model": model,
                        "width": width,
                        "method": "keyframe",
                        "method_params": {},
                    })
                elif method == "uniform":
                    for count in args.frame_counts:
                        matrix.append({
                            "model": model,
                            "width": width,
                            "method": "uniform",
                            "method_params": {"count": count},
                        })

    return matrix


# ---------------------------------------------------------------------------
# Core test runner
# ---------------------------------------------------------------------------


def run_single_test(
    video_path: Path,
    video_label: str,
    config: dict,
    prompt: str,
    ollama_host: str,
    ollama_timeout: float,
    work_dir: Path,
    num_ctx: int | None = None,
    hwaccel: str | None = None,
    motion_mask_bool: np.ndarray | None = None,
    extract_only: bool = False,
    extracted_frames_root: Path | None = None,
) -> dict:
    """Run a single extraction + analysis test, return result dict."""
    method = config["method"]
    params = config["method_params"]
    width = config["width"] or None
    model = config["model"]

    param_desc = ", ".join(f"{k}={v}" for k, v in params.items())
    accel_label = f" [{hwaccel}]" if hwaccel else ""
    _info(
        f"[{model}] {method}({param_desc}) w={width}{accel_label} -> {video_label}"
    )

    frame_dir = work_dir / f"frames_{method}_{hash(json.dumps(params, sort_keys=True)) & 0xFFFF:04x}"
    frame_dir.mkdir(parents=True, exist_ok=True)

    t_extract = time.monotonic()

    if method == "interval":
        frames = extract_frames_interval(
            video_path, frame_dir, params["interval"],
            hwaccel=hwaccel, width=width,
        )
    elif method == "motion":
        motion_timing: dict | None = {}
        frames = extract_frames_motion(
            video_path, frame_dir, params["threshold"], params.get("sample_rate", 0.5),
            hwaccel=hwaccel, mask_bool=motion_mask_bool, width=width,
            cleanup_candidates=extract_only,
            timing_out=motion_timing,
        )
    elif method == "keyframe":
        frames = extract_frames_keyframe(video_path, frame_dir, hwaccel=hwaccel, width=width)
    elif method == "uniform":
        frames = extract_frames_uniform(
            video_path, frame_dir, params["count"],
            hwaccel=hwaccel, width=width,
        )
    else:
        _err(f"Unknown method: {method}")
        frames = []

    duration_extract = time.monotonic() - t_extract

    result = {
        "video": video_label,
        "model": model,
        "method": method,
        "method_params": params,
        "width": width,
        "hwaccel": hwaccel,
        "frames_extracted": len(frames),
        "total_image_bytes": 0,
        "prompt": prompt,
        "response": None,
        "duration_extract_s": round(duration_extract, 2),
        "duration_llm_s": 0,
        "duration_total_s": 0,
        "ollama_eval_count": None,
        "ollama_prompt_eval_count": None,
        "ollama_total_duration_ns": None,
        "ollama_eval_duration_ns": None,
        "error": None,
    }
    if method == "motion":
        result["extract_stages_s"] = motion_timing

    if not frames:
        result["error"] = "No frames extracted"
        _err("No frames extracted, skipping LLM call")
        return result

    if extract_only:
        result["duration_total_s"] = round(duration_extract, 2)
        if extracted_frames_root is not None:
            relative_frames: list[str] = []
            for fp in sorted(frames):
                try:
                    relative_frames.append(str(fp.relative_to(extracted_frames_root)))
                except ValueError:
                    relative_frames.append(str(fp))
            result["saved_frames"] = relative_frames
            result["saved_frames_dir"] = str(frame_dir)
        _info(f"  Extract-only mode: saved {len(frames)} frame(s), skipping LLM call")
        return result

    images_b64, total_bytes = load_and_encode_frames(frames)
    result["total_image_bytes"] = total_bytes
    _info(f"  {len(images_b64)} frames, {total_bytes / 1024:.0f} KB image data")

    if not images_b64:
        result["error"] = "All frames failed to encode"
        return result

    llm_result = call_ollama(ollama_host, model, prompt, images_b64, ollama_timeout, num_ctx)

    result["response"] = llm_result.get("response")
    result["model"] = llm_result.get("model_used", model)
    result["duration_llm_s"] = round(llm_result.get("duration_llm_s", 0), 2)
    result["ollama_eval_count"] = llm_result.get("ollama_eval_count")
    result["ollama_prompt_eval_count"] = llm_result.get("ollama_prompt_eval_count")
    result["ollama_total_duration_ns"] = llm_result.get("ollama_total_duration_ns")
    result["ollama_eval_duration_ns"] = llm_result.get("ollama_eval_duration_ns")
    result["error"] = llm_result.get("error")
    result["duration_total_s"] = round(duration_extract + result["duration_llm_s"], 2)

    if result["error"]:
        _err(f"  LLM error: {result['error']}")
    else:
        resp_preview = (result["response"] or "")[:80]
        _info(f"  LLM: {result['duration_llm_s']:.1f}s, {result.get('ollama_eval_count') or '?'} tokens")
        _info(f"  -> {resp_preview}...")

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

DEFAULT_PROMPT = (
    "You are analyzing frames extracted from a security camera video of an "
    "intrusion detection event. Describe concisely what you see across the "
    "frames: people, vehicles, animals, movement patterns, or other notable "
    "activity. Keep the response to a few short sentences. "
    "Ignore weather conditions and overlay timestamp."
)


def parse_csv_str(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def parse_csv_float(value: str) -> list[float]:
    return [float(v.strip()) for v in value.split(",") if v.strip()]


def parse_csv_int(value: str) -> list[int]:
    return [int(v.strip()) for v in value.split(",") if v.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Compare video analysis approaches with Ollama vision models.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  %(prog)s --media-path /media
  %(prog)s --videos /path/to/clip.dav --models qwen3-vl:8b,llava:13b
  %(prog)s --media-path /media --methods interval,motion --widths 512,1024
  %(prog)s --media-path /media --dry-run
""",
    )

    src = p.add_mutually_exclusive_group()
    src.add_argument(
        "--videos", type=str, default=None,
        help="Comma-separated video file paths (DAV or MP4).",
    )
    src.add_argument(
        "--media-path", type=str, default=None,
        help="Camera media directory to auto-discover DAV files from.",
    )

    p.add_argument("--max-videos", type=int, default=3,
                    help="Max videos to test when using --media-path (default: 3).")
    p.add_argument("--models", type=str, default="qwen3-vl:8b",
                    help="Comma-separated Ollama model names (default: qwen3-vl:8b).")
    p.add_argument("--methods", type=str, default="interval,motion,keyframe,uniform",
                    help="Comma-separated extraction methods (default: all four).")
    p.add_argument("--intervals", type=str, default="1,2,5",
                    help="Comma-separated intervals in seconds for 'interval' method (default: 1,2,5).")
    p.add_argument("--motion-thresholds", type=str, default="0.01,0.02,0.05",
                    help="Comma-separated pixel-diff thresholds (0-1) for 'motion' method (default: 0.01,0.02,0.05).")
    p.add_argument("--motion-sample-rate", type=float, default=0.5,
                    help="Candidate frame sampling interval in seconds for 'motion' method (default: 0.5).")
    p.add_argument("--frame-counts", type=str, default="3,5,10",
                    help="Comma-separated frame counts for 'uniform' method (default: 3,5,10).")
    p.add_argument("--widths", type=str, default="512,768,1024",
                    help="Comma-separated max image widths in px (default: 512,768,1024).")
    p.add_argument("--prompt", type=str, default=DEFAULT_PROMPT,
                    help="Prompt sent to the vision model.")
    p.add_argument("--num-ctx", type=int, default=None,
                    help="Context window size (num_ctx) for Ollama. If not set, uses the model default.")
    p.add_argument("--ollama-host", type=str, default="http://localhost:11434",
                    help="Ollama API base URL (default: http://localhost:11434).")
    p.add_argument("--ollama-timeout", type=float, default=600,
                    help="HTTP timeout for Ollama requests in seconds (default: 600).")
    p.add_argument("--output", type=str, default="test_results.json",
                    help="Output JSON file path (default: test_results.json).")
    p.add_argument("--hwaccel", type=str, default="off",
                    choices=["off", "auto", "vaapi"],
                    help="Hardware acceleration for ffmpeg decoding: off (default), auto, or vaapi.")
    p.add_argument("--motion-mask", type=str, default=None,
                    help="Path to a grayscale mask image for motion detection "
                         "(white=monitor, black=ignore). Must match the frame "
                         "dimensions at the given --widths. "
                         "Applied only to 'motion' method.")
    p.add_argument("--dry-run", action="store_true",
                    help="Show the test matrix without running any tests.")
    p.add_argument("--extract-only", action="store_true",
                    help="Run frame extraction only (skip Ollama analysis) and save extracted frames for review.")
    p.add_argument("--frames-output-dir", type=str, default="extracted_frames_review",
                    help="Directory used to store extracted frames when --extract-only is enabled "
                         "(default: extracted_frames_review).")

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # Parse comma-separated args into typed lists
    args.models = parse_csv_str(args.models)
    args.methods = parse_csv_str(args.methods)
    args.intervals = parse_csv_float(args.intervals)
    args.motion_thresholds = parse_csv_float(args.motion_thresholds)
    args.frame_counts = parse_csv_int(args.frame_counts)
    args.widths = parse_csv_int(args.widths)

    valid_methods = {"interval", "motion", "keyframe", "uniform"}
    for m in args.methods:
        if m not in valid_methods:
            parser.error(f"Unknown method '{m}'. Choose from: {', '.join(sorted(valid_methods))}")

    # Load optional motion mask and convert to boolean array once
    motion_mask_bool: np.ndarray | None = None
    if args.motion_mask:
        mask_path = Path(args.motion_mask)
        if not mask_path.is_file():
            parser.error(f"Motion mask file not found: {mask_path}")
        try:
            mask_img = Image.open(mask_path).convert("L")
            mask_img.load()
            motion_mask_bool = np.asarray(mask_img) > 127
            active = int(motion_mask_bool.sum())
            total = motion_mask_bool.size
            _info(
                f"Motion mask loaded: {mask_path} "
                f"({mask_img.size[0]}x{mask_img.size[1]}, "
                f"{active}/{total} active pixels)"
            )
        except Exception as e:
            parser.error(f"Cannot load motion mask {mask_path}: {e}")

    # Resolve hardware acceleration
    if args.hwaccel == "auto":
        hwaccel = _detect_hwaccel()
        if hwaccel:
            _info(f"Hardware acceleration: {hwaccel}")
        else:
            _info("Hardware acceleration: not available, using software")
    elif args.hwaccel == "vaapi":
        hwaccel = "vaapi"
        _info("Hardware acceleration: vaapi (forced)")
    else:
        hwaccel = None

    # Resolve video files
    video_paths: list[Path] = []

    if args.videos:
        for v in parse_csv_str(args.videos):
            p = Path(v)
            if not p.is_file():
                _err(f"Video file not found: {p}")
            else:
                video_paths.append(p)
    elif args.media_path:
        video_paths = discover_videos(args.media_path, args.max_videos)
    else:
        parser.error("Provide either --videos or --media-path.")

    if not video_paths:
        _err("No video files found.")
        sys.exit(1)

    _info(f"Videos: {len(video_paths)}")
    for vp in video_paths:
        _info(f"  {vp}")

    # Build test matrix
    matrix = build_test_matrix(args)
    total_runs = len(matrix) * len(video_paths)

    _header("TEST MATRIX")
    _info(f"Videos:     {len(video_paths)}")
    _info(f"Models:     {', '.join(args.models)}")
    _info(f"Methods:    {', '.join(args.methods)}")
    _info(f"Widths:     {', '.join(str(w) for w in args.widths)}")
    if motion_mask_bool is not None:
        _info(f"Mask:       {args.motion_mask} ({motion_mask_bool.shape[1]}x{motion_mask_bool.shape[0]})")
    _info(f"Configs:    {len(matrix)} per video")
    _info(f"Total runs: {total_runs}")
    if args.extract_only:
        _info("Mode:       extract-only (LLM disabled)")

    if args.dry_run:
        _header("DRY RUN - Test configurations")
        for i, cfg in enumerate(matrix, 1):
            param_desc = ", ".join(f"{k}={v}" for k, v in cfg["method_params"].items())
            _info(f"  {i:3d}. model={cfg['model']}  method={cfg['method']}({param_desc})  width={cfg['width']}")
        _info(f"\nWould run {total_runs} tests across {len(video_paths)} video(s).")
        return

    # Run tests
    all_results: list[dict] = []
    run_idx = 0
    run_root: Path | None = None
    cleanup_tmp = None
    if args.extract_only:
        run_root = Path(args.frames_output_dir)
        run_root.mkdir(parents=True, exist_ok=True)
        _info(f"Frame output: {run_root.resolve()}")
    else:
        cleanup_tmp = tempfile.TemporaryDirectory(prefix="vidtest_")
        run_root = Path(cleanup_tmp.name)

    assert run_root is not None
    mp4_cache: dict[str, Path] = {}

    try:
        for video_path in video_paths:
            # Prepare the video (convert DAV to MP4 if needed)
            video_key = str(video_path)
            if video_path.suffix.lower() == ".dav":
                if video_key not in mp4_cache:
                    mp4 = convert_dav_to_mp4(video_path, run_root)
                    if mp4 is None:
                        _err(f"Skipping {video_path.name} (conversion failed)")
                        continue
                    mp4_cache[video_key] = mp4
                work_video = mp4_cache[video_key]
            else:
                work_video = video_path

            duration = get_video_duration(work_video)
            video_label = f"{video_path.parent.name}/{video_path.name}"
            _header(f"VIDEO: {video_label} ({duration:.1f}s)" if duration else f"VIDEO: {video_label}")

            safe_video_label = re.sub(r"[^A-Za-z0-9._-]+", "_", video_label)
            video_out_root = run_root / safe_video_label

            for cfg in matrix:
                run_idx += 1
                _info(f"\n--- Run {run_idx}/{total_runs} ---")

                if args.extract_only:
                    work_dir = video_out_root / f"run_{run_idx:04d}"
                else:
                    work_dir = run_root / f"run_{run_idx:04d}"
                work_dir.mkdir(parents=True, exist_ok=True)

                result = run_single_test(
                    video_path=work_video,
                    video_label=video_label,
                    config=cfg,
                    prompt=args.prompt,
                    ollama_host=args.ollama_host,
                    ollama_timeout=args.ollama_timeout,
                    work_dir=work_dir,
                    num_ctx=args.num_ctx,
                    hwaccel=hwaccel,
                    motion_mask_bool=motion_mask_bool,
                    extract_only=args.extract_only,
                    extracted_frames_root=run_root if args.extract_only else None,
                )
                all_results.append(result)
    finally:
        if cleanup_tmp is not None:
            cleanup_tmp.cleanup()

    # Output
    print_results_table(all_results)

    output_path = Path(args.output)
    output_data = {
        "timestamp": datetime.now(tz=None).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "config": {
            "models": args.models,
            "methods": args.methods,
            "widths": args.widths,
            "intervals": args.intervals,
            "motion_thresholds": args.motion_thresholds,
            "motion_sample_rate": args.motion_sample_rate,
            "frame_counts": args.frame_counts,
            "prompt": args.prompt,
            "ollama_host": args.ollama_host,
            "num_ctx": args.num_ctx,
            "hwaccel": hwaccel,
            "motion_mask": args.motion_mask,
            "extract_only": args.extract_only,
            "frames_output_dir": args.frames_output_dir if args.extract_only else None,
        },
        "results": all_results,
    }
    output_path.write_text(json.dumps(output_data, indent=2, ensure_ascii=False))
    _info(f"Results written to {output_path}")


if __name__ == "__main__":
    main()
