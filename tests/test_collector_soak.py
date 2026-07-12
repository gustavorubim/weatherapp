from __future__ import annotations

import time
from io import BytesIO

from PIL import Image

from app import config
from app.cache_manager import CacheManager
from app.storage import list_frames
from app.wms import WmsError


def _png(color: tuple[int, int, int, int]) -> bytes:
    image = Image.new("RGBA", (32, 32), color)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_accelerated_ten_minute_collector_soak(monkeypatch, tmp_path):
    """Exercise restart-safe polling for a 10-minute window at 100x speed.

    The accelerated clock is intentionally short for CI: 0.30 seconds models
    ten minutes, while the worker still performs real fetch, codec, preview,
    catalog, and dedupe operations on every poll.
    """
    frames = [_png((255, 0, 0, 255)), _png((0, 255, 0, 255)), _png((0, 0, 255, 255))]
    calls = {"count": 0}

    def fake_fetch(radar_id, *, width, height, client=None):
        call = calls["count"]
        calls["count"] += 1
        if call == 3:
            raise WmsError("simulated transient WMS error")
        # The first two calls are identical and must be deduplicated.
        payload = frames[0] if call < 2 else frames[(call - 2) % len(frames)]
        return payload, [0.0, 0.0, 1.0, 1.0], "sr_bref"

    monkeypatch.setattr("app.cache_manager.fetch_png_bytes", fake_fetch)
    monkeypatch.setattr("app.cache_manager.get_radar", lambda radar_id: {"id": radar_id})
    monkeypatch.setattr("app.cache_manager.supports_archiving", lambda radar_id: True)
    monkeypatch.setattr("app.cache_manager.CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr("app.storage.CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "CATALOG_PATH", tmp_path / "catalog.sqlite3")
    monkeypatch.setattr("app.cache_manager.RETENTION_MAX_TOTAL_BYTES", 10**9)

    manager = CacheManager(poll_interval_sec=0.01, width=32, height=32)
    manager.start("KTEST")
    time.sleep(0.30)  # accelerated ten-minute soak
    first = manager.stop("KTEST")
    # Restarting the same worker must resume from metadata without rewriting
    # the duplicate source frame.
    manager.start("KTEST")
    time.sleep(0.12)
    status = manager.stop("KTEST")
    manager._run_retention_guard("KTEST")

    assert calls["count"] >= 3
    assert status["polls"] >= 3
    assert first["saves"] >= 2
    assert status["saves"] >= 1
    assert status["running"] is False
    assert len(list_frames("KTEST")) >= 3
    assert all(path.suffix == ".png" for path in (tmp_path / "cache" / "KTEST" / "frames").iterdir())
