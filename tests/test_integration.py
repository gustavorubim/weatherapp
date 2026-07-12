from __future__ import annotations

import hashlib
import time
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from app import config, main
from app.main import app
from app.storage import save_frame_if_new


def _png_bytes(color: tuple[int, int, int, int]) -> bytes:
    image = Image.new("RGBA", (16, 16), color)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _save_frame(radar_id: str, color: tuple[int, int, int, int]) -> None:
    payload = _png_bytes(color)
    save_frame_if_new(
        radar_id,
        payload,
        hashlib.sha256(payload).hexdigest(),
        width=16,
        height=16,
        bbox_3857=[0, 0, 1, 1],
        poll_interval_sec=75,
    )


def test_frames_api_is_bounded_and_has_preview_contract(tmp_path, monkeypatch):
    monkeypatch.setattr("app.storage.CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    _save_frame("KTEST", (255, 0, 0, 255))
    _save_frame("KTEST", (0, 255, 0, 255))

    client = TestClient(app)
    response = client.get("/api/cache/KTEST/frames?limit=1")

    assert response.status_code == 200
    frames = response.json()
    assert len(frames) == 1
    assert frames[0]["preview_url"].endswith(f"/frame/{frames[0]['filename']}")
    assert frames[0]["url"] == frames[0]["preview_url"]
    assert "fetched_at" in frames[0]


def test_storage_status_and_retention_plan_report_exact_candidate_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr("app.storage.CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    _save_frame("KTEST", (255, 0, 0, 255))
    _save_frame("KTEST", (0, 255, 0, 255))

    client = TestClient(app)
    storage = client.get("/api/storage/status")
    assert storage.status_code == 200
    assert storage.json()["frame_count"] == 2

    current_bytes = storage.json()["bytes"]
    plan = client.post(
        "/api/storage/retention/plan",
        json={"max_total_bytes": max(1, current_bytes - 1)},
    )
    assert plan.status_code == 200
    payload = plan.json()
    assert payload["candidate_count"] >= 1
    assert payload["candidate_bytes"] == sum(item["bytes"] for item in payload["candidates"])


def test_video_job_api_reports_completion_without_blocking_request(tmp_path, monkeypatch):
    output = tmp_path / "KTEST.mp4"

    def fake_export(radar_id, *, start=None, end=None, fps=15):
        output.write_bytes(b"fake mp4")
        return output

    monkeypatch.setattr(main, "export_video", fake_export)
    client = TestClient(app)
    submitted = client.post("/api/videos/jobs", json={"radar_id": "KTEST", "fps": 12})

    assert submitted.status_code == 200
    job_id = submitted.json()["job_id"]
    deadline = time.monotonic() + 2
    status = submitted.json()
    while status["state"] in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.01)
        status = client.get(f"/api/videos/jobs/{job_id}").json()

    assert status["state"] == "complete"
    assert status["download_url"].endswith("/KTEST.mp4")
    assert output.exists()
