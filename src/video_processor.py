"""Video download (yt-dlp) and frame extraction (ffmpeg)."""

import os
import subprocess
import tempfile
from pathlib import Path
from typing import List


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def ts_to_sec(ts: str) -> int:
    """Convert HH:MM:SS or MM:SS to integer seconds."""
    parts = ts.strip().split(":")
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + int(s)
    elif len(parts) == 2:
        m, s = parts
        return int(m) * 60 + int(s)
    else:
        return int(float(parts[0]))


def sec_to_ts(sec: int) -> str:
    """Convert integer seconds to HH:MM:SS string."""
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Video download
# ---------------------------------------------------------------------------

def download_segment(url: str, start: str, end: str, output_path: str) -> str:
    """
    Download a time-coded segment from a YouTube URL using yt-dlp.

    Falls back to full-download + ffmpeg trim if section download fails
    (some live VODs don't support section downloads).
    """
    start_sec = ts_to_sec(start)
    end_sec   = ts_to_sec(end)

    # Primary: yt-dlp section download (fast)
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]/best",
        "--download-sections", f"*{start_sec}-{end_sec}",
        "--force-keyframes-at-cuts",
        "-o", output_path,
        "--no-warnings",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)

    if result.returncode == 0 and os.path.exists(output_path):
        return output_path

    # Fallback: download full video then trim with ffmpeg
    raw_path = output_path + "_raw.mp4"
    cmd_full = [
        "yt-dlp",
        "--no-playlist",
        "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]/best",
        "-o", raw_path,
        "--no-warnings",
        url,
    ]
    subprocess.run(cmd_full, check=True, timeout=1800)

    # Trim with ffmpeg
    duration = end_sec - start_sec
    cmd_trim = [
        "ffmpeg", "-y",
        "-ss", str(start_sec),
        "-i", raw_path,
        "-t", str(duration),
        "-c", "copy",
        output_path,
    ]
    subprocess.run(cmd_trim, check=True, timeout=120)
    os.remove(raw_path)

    return output_path


# ---------------------------------------------------------------------------
# Frame extraction
# ---------------------------------------------------------------------------

def extract_frames(video_path: str, interval_sec: int = 20,
                   start: str = None, end: str = None) -> List[str]:
    """
    Extract JPEG frames from a video every *interval_sec* seconds.

    Optional *start*/*end* (HH:MM:SS or MM:SS) trim to a segment — useful when a
    single recording contains several maps.

    Returns sorted list of frame file paths in a temp directory.
    Uses -q:v 2 (high quality) since clean text matters for OCR.
    """
    out_dir = tempfile.mkdtemp(prefix="r6m_frames_")
    pattern = os.path.join(out_dir, "frame_%05d.jpg")

    cmd = ["ffmpeg", "-y"]
    if start:
        cmd += ["-ss", str(ts_to_sec(start))]      # input seeking (fast)
    cmd += ["-i", video_path]
    if end:
        dur = ts_to_sec(end) - (ts_to_sec(start) if start else 0)
        cmd += ["-t", str(dur)]
    cmd += [
        "-vf", f"fps=1/{interval_sec}",
        "-q:v", "2",          # high JPEG quality — small HUD text needs clarity
        "-vsync", "vfr",
        pattern,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg frame extraction failed:\n{result.stderr[-500:]}")

    frames = sorted(Path(out_dir).glob("frame_*.jpg"))
    return [str(f) for f in frames]


def cleanup_dir(frame_paths: List[str]):
    """Delete temp frame files and their parent directory."""
    import shutil
    if frame_paths:
        parent = os.path.dirname(frame_paths[0])
        shutil.rmtree(parent, ignore_errors=True)
