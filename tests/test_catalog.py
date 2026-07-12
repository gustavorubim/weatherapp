from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image

from app.catalog import Catalog, FrameRecord
from app.catalog_cli import rebuild


def record(name: str, *, radar: str = "KTBW", observed: str | None = None, size: int = 4, pinned: bool = False) -> FrameRecord:
    digest = hashlib.sha256(name.encode()).hexdigest()
    return FrameRecord(
        radar_id=radar,
        filename=name,
        path=f"/tmp/{radar}/{name}",
        preview_path=None,
        product="sr_bref",
        observed_at=observed,
        fetched_at="2026-07-11T12:00:00Z",
        width=2,
        height=2,
        media_type="image/png",
        source_sha256=digest,
        stored_sha256=digest,
        bytes=size,
        pinned=pinned,
    )


def test_catalog_schema_upsert_pagination_and_stats(tmp_path: Path) -> None:
    db = tmp_path / "catalog.sqlite3"
    with Catalog(db) as catalog:
        catalog.record_frame(record("03.png", observed="2026-07-11T12:03:00Z", size=3))
        catalog.record_frame(record("01.png", observed=None, size=1))
        catalog.record_frame(record("02.png", observed="2026-07-11T12:02:00Z", size=2))
        catalog.set_pinned("KTBW", "02.png", True)
        catalog.record_frame(record("02.png", observed="2026-07-11T12:02:01Z", size=9))

        assert catalog.count() == 3
        assert catalog.get_frame("ktbw", "02.png").pinned is True
        assert catalog.get_frame("KTBW", "01.png").observed_at is None
        first = catalog.list_frames("KTBW", limit=2)
        assert [item.filename for item in first] == ["01.png", "02.png"]
        second = catalog.list_frames("KTBW", after=first[-1].filename, limit=2)
        assert [item.filename for item in second] == ["03.png"]
        assert catalog.radar_stats("KTBW")["frame_count"] == 3
        assert catalog.radar_stats("KTBW")["bytes"] == 13
        assert catalog.global_stats()["radar_count"] == 1
        assert catalog.verify()["ok"]


def test_rebuild_indexes_legacy_files_without_moving_them(tmp_path: Path) -> None:
    frames = tmp_path / "cache" / "KTBW" / "frames"
    frames.mkdir(parents=True)
    image_path = frames / "20260711_120000Z.png"
    Image.new("RGBA", (3, 4), (20, 30, 40, 255)).save(image_path)
    original = image_path.read_bytes()

    dry = rebuild(tmp_path / "cache", tmp_path / "catalog.sqlite3", dry_run=True)
    assert dry["records"] == 1
    assert not (tmp_path / "catalog.sqlite3").exists()

    result = rebuild(tmp_path / "cache", tmp_path / "catalog.sqlite3", dry_run=False)
    assert result["catalog_count"] == 1
    assert image_path.read_bytes() == original
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        frame = catalog.latest_frame("KTBW")
        assert frame is not None
        assert frame.observed_at is None
        assert frame.width == 3 and frame.height == 4
        assert frame.bytes == len(original)
