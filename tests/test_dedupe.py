from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image

from app.storage import list_frames, load_metadata, save_frame_if_new


def _png_bytes(color: tuple[int, int, int, int], size: tuple[int, int] = (64, 64)) -> bytes:
    from io import BytesIO

    img = Image.new("RGBA", size, color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_dedupe_skips_identical_hash(tmp_path, monkeypatch):
    monkeypatch.setattr("app.storage.CACHE_DIR", tmp_path)
    radar = "KTEST"
    a = _png_bytes((255, 0, 0, 255))
    b = _png_bytes((255, 0, 0, 255))
    assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()

    path1, meta1, saved1 = save_frame_if_new(
        radar,
        a,
        hashlib.sha256(a).hexdigest(),
        width=64,
        height=64,
        bbox_3857=[0, 0, 1, 1],
        poll_interval_sec=75,
    )
    assert saved1 is True
    assert path1 is not None
    assert meta1["frame_count"] == 1

    path2, meta2, saved2 = save_frame_if_new(
        radar,
        b,
        hashlib.sha256(b).hexdigest(),
        width=64,
        height=64,
        bbox_3857=[0, 0, 1, 1],
        poll_interval_sec=75,
    )
    assert saved2 is False
    assert path2 is None
    assert meta2["frame_count"] == 1
    assert len(list((tmp_path / radar / "frames").glob("*.png"))) == 1


def test_dedupe_saves_when_hash_changes(tmp_path, monkeypatch):
    monkeypatch.setattr("app.storage.CACHE_DIR", tmp_path)
    radar = "KTEST"
    a = _png_bytes((255, 0, 0, 255))
    c = _png_bytes((0, 255, 0, 255))
    assert hashlib.sha256(a).hexdigest() != hashlib.sha256(c).hexdigest()

    save_frame_if_new(
        radar,
        a,
        hashlib.sha256(a).hexdigest(),
        width=64,
        height=64,
        bbox_3857=[0, 0, 1, 1],
        poll_interval_sec=75,
    )
    path2, meta2, saved2 = save_frame_if_new(
        radar,
        c,
        hashlib.sha256(c).hexdigest(),
        width=64,
        height=64,
        bbox_3857=[0, 0, 1, 1],
        poll_interval_sec=75,
    )
    assert saved2 is True
    assert path2 is not None
    assert meta2["frame_count"] == 2
    frames = list_frames(radar)
    assert len(frames) == 2
    meta = load_metadata(radar)
    assert meta["last_sha256"] == hashlib.sha256(c).hexdigest()
    assert Path(meta_path := tmp_path / radar / "metadata.json").exists()
    assert meta_path.read_text()
