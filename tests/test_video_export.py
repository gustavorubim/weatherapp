from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app.video import VideoError, export_video, probe_frame_dimensions, resolve_quality


def _write_png(path: Path, size: tuple[int, int], color: tuple[int, int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", size, color).save(path, format="PNG", optimize=False)


def _frame(path: Path, name: str, utc: str) -> dict:
    from datetime import datetime, timezone

    ts = datetime.fromisoformat(utc.replace("Z", "+00:00"))
    return {
        "filename": name,
        "path": str(path / name),
        "utc": utc,
        "timestamp": ts,
        "size": (path / name).stat().st_size,
    }


@pytest.fixture
def uniform_frames(tmp_path: Path):
    root = tmp_path / "frames"
    names = [
        ("20260711_210000Z.png", "2026-07-11T21:00:00Z", (10, 20, 30, 255)),
        ("20260711_210100Z.png", "2026-07-11T21:01:00Z", (40, 50, 60, 255)),
        ("20260711_210200Z.png", "2026-07-11T21:02:00Z", (70, 80, 90, 255)),
    ]
    frames = []
    for name, utc, color in names:
        # Large enough that concat-manifest overhead stays << 5% of source bytes.
        _write_png(root / name, (256, 256), color)
        frames.append(_frame(root, name, utc))
    return frames


@pytest.fixture
def mixed_frames(tmp_path: Path):
    root = tmp_path / "mixed"
    specs = [
        ("20260711_210000Z.png", "2026-07-11T21:00:00Z", (1024, 1024), (10, 20, 30, 255)),
        ("20260711_210100Z.png", "2026-07-11T21:01:00Z", (2048, 2048), (40, 50, 60, 255)),
        ("20260711_210200Z.png", "2026-07-11T21:02:00Z", (2048, 2048), (70, 80, 90, 255)),
    ]
    frames = []
    for name, utc, size, color in specs:
        _write_png(root / name, size, color)
        frames.append(_frame(root, name, utc))
    return frames


def test_quality_presets():
    crf, preset = resolve_quality("balanced")
    assert crf == 18
    assert preset == "medium"
    assert resolve_quality("archive")[0] == 15
    assert resolve_quality("small")[0] == 26
    with pytest.raises(VideoError):
        resolve_quality("nope")


def test_probe_groups_mixed(mixed_frames):
    groups = probe_frame_dimensions(mixed_frames)
    assert (1024, 1024) in groups
    assert (2048, 2048) in groups
    assert len(groups[(1024, 1024)]) == 1
    assert len(groups[(2048, 2048)]) == 2


def test_export_uniform_compatible(uniform_frames, tmp_path):
    out = tmp_path / "out.mp4"
    path = export_video(
        "KTEST",
        frames_override=uniform_frames,
        fps=8,
        out=out,
        quality="balanced",
    )
    assert path.exists()
    assert path.stat().st_size > 0
    # Overhead must stay tiny vs source
    assert export_video.last_temp_overhead_bytes / max(export_video.last_source_bytes, 1) < 0.05
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    info = json.loads(probe.stdout)
    stream = info["streams"][0]
    assert stream["codec_name"] == "h264"
    assert int(stream["width"]) == 256
    assert int(stream["height"]) == 256


def test_mixed_dimensions_error_lists_groups(mixed_frames, tmp_path):
    with pytest.raises(VideoError) as exc:
        export_video(
            "KTEST",
            frames_override=mixed_frames,
            out=tmp_path / "mixed.mp4",
            dimension_policy="error",
        )
    msg = str(exc.value)
    assert "1024x1024" in msg
    assert "2048x2048" in msg
    assert "dimension_policy" in msg


def test_mixed_dimensions_normalize(mixed_frames, tmp_path):
    out = tmp_path / "norm.mp4"
    path = export_video(
        "KTEST",
        frames_override=mixed_frames,
        out=out,
        dimension_policy="normalize",
        target_width=512,
        target_height=512,
        quality="small",
        fps=6,
    )
    assert path.exists()
    assert export_video.last_output_size == (512, 512)
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert int(stream["width"]) == 512
    assert int(stream["height"]) == 512


def test_output_name_has_precision_and_suffix(uniform_frames, tmp_path, monkeypatch):
    monkeypatch.setattr("app.video.VIDEOS_DIR", tmp_path)
    path = export_video("KTEST", frames_override=uniform_frames, fps=10, quality="archive")
    name = path.name
    assert "KTEST_" in name
    assert "T" in name  # ISO-like precision in filename
    assert "archive" in name
    assert name.endswith(".mp4")
    # content suffix present (8 hex)
    stem = path.stem
    assert len(stem.split("_")[-1]) == 8


def test_all_presets_produce_mp4(uniform_frames, tmp_path):
    for quality in ("archive", "balanced", "small"):
        out = tmp_path / f"{quality}.mp4"
        path = export_video(
            "KTEST",
            frames_override=uniform_frames,
            out=out,
            quality=quality,
            fps=5,
        )
        assert path.exists() and path.stat().st_size > 0
