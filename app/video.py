from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from app.config import VIDEOS_DIR, ensure_dirs
from app.storage import list_frames, parse_iso_utc

logger = logging.getLogger(__name__)

FFMPEG_INSTALL_HINT = (
    "ffmpeg is required for video export. Install it first, e.g.:\n"
    "  macOS:  brew install ffmpeg\n"
    "  Ubuntu: sudo apt install ffmpeg\n"
    "  Windows: https://ffmpeg.org/download.html"
)


class VideoError(RuntimeError):
    pass


def ensure_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise VideoError(FFMPEG_INSTALL_HINT)
    return path


def _parse_bound(value: str | datetime | None, *, end_of_day: bool = False) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = value.strip()
    if "T" not in text and end_of_day:
        text = f"{text}T23:59:59Z"
    elif "T" not in text:
        text = f"{text}T00:00:00Z"
    return parse_iso_utc(text)


def export_video(
    radar_id: str,
    *,
    start: str | datetime | None = None,
    end: str | datetime | None = None,
    fps: float = 15,
    out: Path | None = None,
    crf: int = 18,
    preset: str = "slow",
) -> Path:
    ensure_dirs()
    ffmpeg = ensure_ffmpeg()
    rid = radar_id.strip().upper()

    start_dt = _parse_bound(start, end_of_day=False)
    end_dt = _parse_bound(end, end_of_day=True)

    frames = list_frames(rid, start=start_dt, end=end_dt)
    if len(frames) < 2:
        raise VideoError(f"Need at least 2 frames to make a video (found {len(frames)} for {rid})")

    start_tag = (start_dt or frames[0]["timestamp"]).strftime("%Y%m%d")
    end_tag = (end_dt or frames[-1]["timestamp"]).strftime("%Y%m%d")
    if out is None:
        out = VIDEOS_DIR / f"{rid}_{start_tag}_{end_tag}_{int(fps)}fps.mp4"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    # Stage sequentially named copies so image2 demuxer + -framerate works reliably.
    with tempfile.TemporaryDirectory(prefix="radarvault_frames_") as tmp:
        tmp_dir = Path(tmp)
        for i, fr in enumerate(frames):
            dest = tmp_dir / f"frame_{i:06d}.png"
            shutil.copy2(fr["path"], dest)

        pattern = str(tmp_dir / "frame_%06d.png")
        cmd = [
            ffmpeg,
            "-y",
            "-framerate",
            str(fps),
            "-i",
            pattern,
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            str(min(int(crf), 20)),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(out),
        ]

        logger.info("Running ffmpeg for %s (%d frames @ %sfps)", rid, len(frames), fps)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise VideoError(f"ffmpeg failed:\n{proc.stderr[-2000:]}")

    if not out.exists() or out.stat().st_size == 0:
        raise VideoError("ffmpeg completed but output file is missing/empty")
    return out
