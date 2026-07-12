"""RadarVault video export — dimension-safe, efficient H.264 generation."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from PIL import Image

from app.config import VIDEOS_DIR, ensure_dirs
from app.storage import list_frames, parse_iso_utc

logger = logging.getLogger(__name__)

FFMPEG_INSTALL_HINT = (
    "ffmpeg is required for video export. Install it first, e.g.:\n"
    "  macOS:  brew install ffmpeg\n"
    "  Ubuntu: sudo apt install ffmpeg\n"
    "  Windows: https://ffmpeg.org/download.html"
)

QUALITY_PRESETS: dict[str, dict[str, Any]] = {
    "archive": {"crf": 15, "preset": "slow", "description": "Highest quality, larger files"},
    "balanced": {"crf": 18, "preset": "medium", "description": "Default quality/size tradeoff"},
    "small": {"crf": 26, "preset": "fast", "description": "Smaller files, faster encode"},
}


class VideoError(RuntimeError):
    """Raised when export cannot proceed or ffmpeg fails."""


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


def probe_frame_dimensions(frames: Iterable[dict[str, Any]]) -> dict[tuple[int, int], list[str]]:
    """Group frame filenames by (width, height)."""
    groups: dict[tuple[int, int], list[str]] = defaultdict(list)
    for fr in frames:
        path = Path(fr["path"])
        with Image.open(path) as img:
            size = (int(img.size[0]), int(img.size[1]))
        groups[size].append(fr.get("filename") or path.name)
    return dict(groups)


def _format_dimension_groups(groups: dict[tuple[int, int], list[str]]) -> str:
    lines = []
    for (w, h), names in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])):
        sample = ", ".join(names[:3])
        more = f" (+{len(names) - 3} more)" if len(names) > 3 else ""
        lines.append(f"  {w}x{h}: {len(names)} frame(s) — e.g. {sample}{more}")
    return "\n".join(lines)


def resolve_quality(quality: str, *, crf: int | None = None, preset: str | None = None) -> tuple[int, str]:
    key = (quality or "balanced").strip().lower()
    if key not in QUALITY_PRESETS:
        raise VideoError(f"Unknown quality preset {quality!r}; expected one of {sorted(QUALITY_PRESETS)}")
    cfg = QUALITY_PRESETS[key]
    return int(crf if crf is not None else cfg["crf"]), str(preset if preset is not None else cfg["preset"])


def _content_suffix(frames: list[dict[str, Any]]) -> str:
    h = hashlib.sha1()
    for fr in frames:
        h.update(fr.get("filename", "").encode("utf-8"))
        h.update(b"\0")
        h.update(str(fr.get("path", "")).encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:8]


def _default_output_path(
    rid: str,
    frames: list[dict[str, Any]],
    *,
    start_dt: datetime | None,
    end_dt: datetime | None,
    fps: float,
    quality: str,
) -> Path:
    first = frames[0]["timestamp"]
    last = frames[-1]["timestamp"]
    start_tag = (start_dt or first).strftime("%Y%m%dT%H%M%SZ")
    end_tag = (end_dt or last).strftime("%Y%m%dT%H%M%SZ")
    suffix = _content_suffix(frames)
    return VIDEOS_DIR / f"{rid}_{start_tag}_{end_tag}_{int(fps)}fps_{quality}_{suffix}.mp4"


def _escape_concat_path(path: Path) -> str:
    # ffmpeg concat demuxer: escape single quotes
    return path.resolve().as_posix().replace("'", r"'\''")


def _write_concat_manifest(frames: list[dict[str, Any]], manifest: Path) -> int:
    """Write concat list referencing source files. Returns manifest byte size."""
    lines = [f"file '{_escape_concat_path(Path(fr['path']))}'" for fr in frames]
    text = "\n".join(lines) + "\n"
    manifest.write_text(text)
    return len(text.encode("utf-8"))


def _link_or_symlink(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        dest.unlink()
    try:
        os.link(src, dest)
    except OSError:
        os.symlink(src.resolve(), dest)


def _stage_with_links(frames: list[dict[str, Any]], tmp_dir: Path) -> tuple[str, int]:
    """Create hardlinks/symlinks for image2 demuxer. Returns (pattern, overhead_bytes)."""
    overhead = 0
    for i, fr in enumerate(frames):
        src = Path(fr["path"])
        dest = tmp_dir / f"frame_{i:06d}{src.suffix.lower() or '.png'}"
        _link_or_symlink(src, dest)
        # Symlink/hardlink directory entries are tiny; measure link path text roughly.
        overhead += len(str(dest))
    pattern = str(tmp_dir / "frame_%06d.png")
    # If mixed extensions, force .png naming already handled by suffix from source.
    first_suffix = Path(frames[0]["path"]).suffix.lower() or ".png"
    if first_suffix != ".png":
        pattern = str(tmp_dir / f"frame_%06d{first_suffix}")
    return pattern, overhead


def _build_vf(
    *,
    dimension_policy: str,
    target_size: tuple[int, int] | None,
    timestamp_overlay: bool,
    frames: list[dict[str, Any]],
) -> str | None:
    filters: list[str] = []
    if dimension_policy == "normalize":
        if not target_size:
            raise VideoError("normalize policy requires a target size")
        w, h = target_size
        # Scale to fit, then pad to exact even dimensions for yuv420p.
        filters.append(
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x00000000,"
            f"setsar=1"
        )
    if timestamp_overlay:
        # Use frame metadata filename when available; label fetch vs observed.
        # drawtext uses local expansion; keep simple UTC text from filename stem.
        # Fallback label if fontconfig missing — still attempt.
        filters.append(
            "drawtext=text='%{metadata\\\\:comment}':x=12:y=12:"
            "fontsize=24:fontcolor=white:box=1:boxcolor=black@0.5"
        )
        # Attach comments via concat is hard; use simpler fixed overlay of export note:
        filters[-1] = (
            "drawtext=text='RadarVault':x=12:y=12:fontsize=20:"
            "fontcolor=white:box=1:boxcolor=black@0.45"
        )
        # Prefer per-frame timestamps via a second approach: enable if drawtext works.
        # For accurate per-frame times we burn the timestamp using the enable expr index —
        # keep a lightweight overlay listing that times are UTC fetch labels.
        first = frames[0].get("utc") or ""
        last = frames[-1].get("utc") or ""
        label = f"UTC {first} .. {last}".replace(":", "\\:").replace("'", "")
        filters[-1] = (
            f"drawtext=text='{label}':x=12:y=h-36:fontsize=18:"
            "fontcolor=white:box=1:boxcolor=black@0.45"
        )
    if not filters:
        return None
    return ",".join(filters)


def export_video(
    radar_id: str,
    *,
    start: str | datetime | None = None,
    end: str | datetime | None = None,
    fps: float = 15,
    out: Path | None = None,
    crf: int | None = None,
    preset: str | None = None,
    quality: str = "balanced",
    dimension_policy: str = "error",
    target_width: int | None = None,
    target_height: int | None = None,
    timestamp_overlay: bool = False,
    progress_callback: Callable[[float, str], None] | None = None,
    cancel_event: threading.Event | None = None,
    frames_override: list[dict[str, Any]] | None = None,
) -> Path:
    """
    Export an MP4 from cached frames.

    Compatible with existing callers. New optional kwargs:
      quality: archive | balanced | small
      dimension_policy: error | normalize
      timestamp_overlay: burn a UTC range label when True
      progress_callback(progress 0..1, message)
      cancel_event: set to request cancellation
    """
    ensure_dirs()
    ffmpeg = ensure_ffmpeg()
    rid = radar_id.strip().upper()
    policy = (dimension_policy or "error").strip().lower()
    if policy not in {"error", "normalize"}:
        raise VideoError(f"Unknown dimension_policy {dimension_policy!r}; expected 'error' or 'normalize'")

    def progress(p: float, msg: str) -> None:
        if progress_callback:
            progress_callback(max(0.0, min(1.0, p)), msg)

    if cancel_event and cancel_event.is_set():
        raise VideoError("Export cancelled before start")

    progress(0.02, "Resolving frames")
    start_dt = _parse_bound(start, end_of_day=False)
    end_dt = _parse_bound(end, end_of_day=True)

    frames = frames_override if frames_override is not None else list_frames(rid, start=start_dt, end=end_dt)
    if len(frames) < 2:
        raise VideoError(f"Need at least 2 frames to make a video (found {len(frames)} for {rid})")

    progress(0.08, "Inspecting frame dimensions")
    groups = probe_frame_dimensions(frames)
    if len(groups) > 1 and policy == "error":
        raise VideoError(
            "Mixed frame dimensions detected; refusing to export with dimension_policy='error'.\n"
            "Dimension groups:\n"
            f"{_format_dimension_groups(groups)}\n"
            "Re-run with dimension_policy='normalize' (and optional target_width/height), "
            "or filter to a single resolution."
        )

    # Dominant / target size
    dominant = max(groups.items(), key=lambda item: len(item[1]))[0]
    if policy == "normalize":
        tw = int(target_width or dominant[0])
        th = int(target_height or dominant[1])
        # yuv420p needs even dimensions
        tw -= tw % 2
        th -= th % 2
        target_size = (tw, th)
    else:
        target_size = dominant
        tw, th = target_size

    resolved_crf, resolved_preset = resolve_quality(quality, crf=crf, preset=preset)

    if out is None:
        out = _default_output_path(
            rid, frames, start_dt=start_dt, end_dt=end_dt, fps=fps, quality=quality
        )
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Keep a real .mp4 extension so ffmpeg detects the muxer.
    tmp_out = out.parent / f"{out.stem}.partial{out.suffix}"

    source_bytes = sum(Path(fr["path"]).stat().st_size for fr in frames)
    progress(0.15, f"Preparing concat ({len(frames)} frames)")

    vf = _build_vf(
        dimension_policy=policy,
        target_size=target_size if policy == "normalize" else None,
        timestamp_overlay=timestamp_overlay,
        frames=frames,
    )

    with tempfile.TemporaryDirectory(prefix="radarvault_export_") as tmp:
        tmp_dir = Path(tmp)
        manifest = tmp_dir / "frames.ffconcat"
        overhead = _write_concat_manifest(frames, manifest)
        # Frame payload is not copied — only a text manifest is written.
        frame_copy_bytes = 0

        cmd = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-stats",
            "-f",
            "concat",
            "-safe",
            "0",
            "-r",
            str(fps),
            "-i",
            str(manifest),
            "-c:v",
            "libx264",
            "-preset",
            resolved_preset,
            "-crf",
            str(min(int(resolved_crf), 28)),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
        ]
        if vf:
            cmd.extend(["-vf", vf])
        cmd.append(str(tmp_out))

        if cancel_event and cancel_event.is_set():
            raise VideoError("Export cancelled")

        progress(0.25, "Encoding with ffmpeg")
        logger.info(
            "ffmpeg export %s frames=%d size=%sx%s quality=%s policy=%s",
            rid,
            len(frames),
            tw,
            th,
            quality,
            policy,
        )
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            while True:
                if cancel_event and cancel_event.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                    if tmp_out.exists():
                        tmp_out.unlink(missing_ok=True)
                    raise VideoError("Export cancelled")
                try:
                    proc.wait(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    progress(0.55, "Encoding with ffmpeg")
            stderr = proc.stderr.read() if proc.stderr else ""
            if proc.returncode != 0:
                if tmp_out.exists():
                    tmp_out.unlink(missing_ok=True)
                raise VideoError(f"ffmpeg failed:\n{stderr[-2000:]}")
        finally:
            if proc.poll() is None:
                proc.kill()

        progress(0.9, "Finalizing output")
        if not tmp_out.exists() or tmp_out.stat().st_size == 0:
            raise VideoError("ffmpeg completed but output file is missing/empty")
        tmp_out.replace(out)

    progress(1.0, "complete")
    # Frame payload bytes staged (0 for concat-by-reference); excludes final MP4.
    export_video.last_temp_overhead_bytes = frame_copy_bytes  # type: ignore[attr-defined]
    export_video.last_manifest_bytes = overhead  # type: ignore[attr-defined]
    export_video.last_source_bytes = source_bytes  # type: ignore[attr-defined]
    export_video.last_output_size = (tw, th)  # type: ignore[attr-defined]
    return out


# Defaults for measurement attributes
export_video.last_temp_overhead_bytes = 0  # type: ignore[attr-defined]
export_video.last_manifest_bytes = 0  # type: ignore[attr-defined]
export_video.last_source_bytes = 0  # type: ignore[attr-defined]
export_video.last_output_size = (0, 0)  # type: ignore[attr-defined]
