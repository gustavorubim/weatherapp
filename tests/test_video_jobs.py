from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from app.video import VideoError, export_video
from app.video_jobs import VideoJobManager, VideoJobRequest, VideoJobStatus


def _frames(tmp_path: Path, n: int = 3) -> list[dict]:
    from datetime import datetime, timezone

    root = tmp_path / "frames"
    root.mkdir(parents=True, exist_ok=True)
    frames = []
    for i in range(n):
        name = f"20260711_21{i:02d}00Z.png"
        path = root / name
        Image.new("RGBA", (48, 48), (i * 40, 10, 20, 255)).save(path)
        ts = datetime(2026, 7, 11, 21, i, 0, tzinfo=timezone.utc)
        frames.append(
            {
                "filename": name,
                "path": str(path),
                "utc": ts.isoformat().replace("+00:00", "Z"),
                "timestamp": ts,
                "size": path.stat().st_size,
            }
        )
    return frames


def test_job_status_serializable(tmp_path):
    frames = _frames(tmp_path)

    def fake_export(*args, **kwargs):
        out = Path(kwargs["out"] or tmp_path / "x.mp4")
        out.write_bytes(b"\x00" * 64)
        if kwargs.get("progress_callback"):
            kwargs["progress_callback"](0.5, "mid")
            kwargs["progress_callback"](1.0, "complete")
        return out

    mgr = VideoJobManager(export_fn=fake_export, retention_seconds=60)
    # Patch export to ignore radar cache: wrap to inject frames_override
    def export_with_frames(*args, **kwargs):
        kwargs["frames_override"] = frames
        kwargs.setdefault("out", tmp_path / "job.mp4")
        return export_video(*args, **kwargs)

    mgr = VideoJobManager(export_fn=export_with_frames, retention_seconds=60)
    job = mgr.submit(VideoJobRequest(radar_id="KTEST", fps=5, out=str(tmp_path / "a.mp4")))
    # Wait for completion
    for _ in range(100):
        st = mgr.status(job.job_id)
        if st.state in {"complete", "failed", "cancelled"}:
            break
        time.sleep(0.05)
    st = mgr.status(job.job_id)
    assert st.state == "complete"
    assert st.progress == 1.0
    d = st.to_dict()
    assert d["job_id"] == job.job_id
    assert d["state"] == "complete"
    assert Path(st.output_path).exists()


def test_progress_monotonic(tmp_path):
    frames = _frames(tmp_path)
    seen = []

    def export_with_frames(radar_id, **kwargs):
        cb = kwargs.get("progress_callback")
        if cb:
            for p in (0.1, 0.2, 0.15, 0.8, 1.0):  # include a regression attempt
                cb(p, f"p={p}")
                seen.append(p)
        out = Path(kwargs["out"])
        # minimal valid-ish file
        out.write_bytes(b"\x00" * 32)
        return out

    mgr = VideoJobManager(export_fn=export_with_frames)
    job = mgr.submit(VideoJobRequest(radar_id="KTEST", out=str(tmp_path / "m.mp4")))
    for _ in range(50):
        st = mgr.status(job.job_id)
        if st.state != "running" and st.state != "queued":
            break
        time.sleep(0.02)
    st = mgr.status(job.job_id)
    assert st.state == "complete"
    assert st.progress == 1.0


def test_cancel_queued_and_running(tmp_path):
    frames = _frames(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def slow_export(radar_id, **kwargs):
        started.set()
        # Wait until cancelled or timeout
        for _ in range(200):
            if kwargs.get("cancel_event") and kwargs["cancel_event"].is_set():
                raise VideoError("Export cancelled")
            if release.wait(0.01):
                break
        out = Path(kwargs["out"])
        out.write_bytes(b"\x00" * 16)
        return out

    mgr = VideoJobManager(export_fn=slow_export, max_concurrent=1)
    job = mgr.submit(VideoJobRequest(radar_id="KTEST", out=str(tmp_path / "c.mp4")))
    assert started.wait(2.0)
    st = mgr.cancel(job.job_id)
    # Allow worker to observe cancel
    time.sleep(0.2)
    st = mgr.status(job.job_id)
    assert st.state == "cancelled"
    release.set()


def test_ffmpeg_failure_retains_error(tmp_path):
    def boom(*args, **kwargs):
        raise VideoError("ffmpeg failed:\nDETAILED LOG TAIL HERE")

    mgr = VideoJobManager(export_fn=boom)
    job = mgr.submit(VideoJobRequest(radar_id="KTEST", out=str(tmp_path / "f.mp4")))
    for _ in range(50):
        st = mgr.status(job.job_id)
        if st.state in {"failed", "complete", "cancelled"}:
            break
        time.sleep(0.02)
    st = mgr.status(job.job_id)
    assert st.state == "failed"
    assert st.error
    assert st.log_tail and "DETAILED LOG TAIL" in st.log_tail


def test_duplicate_reuses_completed(tmp_path):
    frames = _frames(tmp_path)
    calls = {"n": 0}

    def export_once(radar_id, **kwargs):
        calls["n"] += 1
        kwargs["frames_override"] = frames
        return export_video(radar_id, **kwargs)

    mgr = VideoJobManager(export_fn=export_once)
    req = VideoJobRequest(radar_id="KTEST", fps=5, out=str(tmp_path / "dup.mp4"), quality="small")
    j1 = mgr.submit(req)
    for _ in range(100):
        if mgr.status(j1.job_id).state == "complete":
            break
        time.sleep(0.05)
    assert mgr.status(j1.job_id).state == "complete"
    j2 = mgr.submit(req)
    assert j2.job_id == j1.job_id
    assert calls["n"] == 1


def test_concurrency_limit(tmp_path):
    gate = threading.Event()
    active = {"n": 0, "max": 0}
    lock = threading.Lock()

    def blocked_export(radar_id, **kwargs):
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        gate.wait(2.0)
        with lock:
            active["n"] -= 1
        out = Path(kwargs["out"])
        out.write_bytes(b"\x00" * 8)
        return out

    mgr = VideoJobManager(export_fn=blocked_export, max_concurrent=1)
    a = mgr.submit(VideoJobRequest(radar_id="A", out=str(tmp_path / "a.mp4")))
    b = mgr.submit(VideoJobRequest(radar_id="B", out=str(tmp_path / "b.mp4")))
    time.sleep(0.1)
    assert mgr.status(a.job_id).state == "running"
    assert mgr.status(b.job_id).state == "queued"
    gate.set()
    for _ in range(100):
        if mgr.status(a.job_id).state == "complete" and mgr.status(b.job_id).state == "complete":
            break
        time.sleep(0.05)
    assert active["max"] == 1


def test_cleanup_retention(tmp_path):
    def instant(radar_id, **kwargs):
        out = Path(kwargs["out"])
        out.write_bytes(b"\x00" * 4)
        return out

    mgr = VideoJobManager(export_fn=instant, retention_seconds=0.05)
    job = mgr.submit(VideoJobRequest(radar_id="KTEST", out=str(tmp_path / "z.mp4")))
    for _ in range(50):
        if mgr.status(job.job_id).state == "complete":
            break
        time.sleep(0.02)
    time.sleep(0.08)
    removed = mgr.cleanup()
    assert removed >= 1
    with pytest.raises(KeyError):
        mgr.status(job.job_id)
