from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import CACHE_DIR, IMAGE_HEIGHT, IMAGE_WIDTH, POLL_INTERVAL_SEC, PRODUCT, ensure_dirs

FRAME_RE = re.compile(r"^(\d{8}_\d{6})(?:\d{6})?(?:_\d+)?Z\.png$")


def radar_dir(radar_id: str) -> Path:
    return CACHE_DIR / radar_id.strip().upper()


def frames_dir(radar_id: str) -> Path:
    return radar_dir(radar_id) / "frames"


def metadata_path(radar_id: str) -> Path:
    return radar_dir(radar_id) / "metadata.json"


def default_metadata(radar_id: str) -> dict[str, Any]:
    return {
        "radar_id": radar_id.strip().upper(),
        "product": PRODUCT,
        "last_frame_utc": None,
        "last_sha256": None,
        "frame_count": 0,
        "width": IMAGE_WIDTH,
        "height": IMAGE_HEIGHT,
        "poll_interval_sec": POLL_INTERVAL_SEC,
        "bbox_3857": None,
        "disk_bytes": 0,
    }


def load_metadata(radar_id: str) -> dict[str, Any]:
    path = metadata_path(radar_id)
    if not path.exists():
        return default_metadata(radar_id)
    data = json.loads(path.read_text())
    base = default_metadata(radar_id)
    base.update(data)
    return base


def save_metadata(radar_id: str, meta: dict[str, Any]) -> None:
    ensure_dirs()
    rd = radar_dir(radar_id)
    rd.mkdir(parents=True, exist_ok=True)
    frames_dir(radar_id).mkdir(parents=True, exist_ok=True)
    path = metadata_path(radar_id)
    path.write_text(json.dumps(meta, indent=2))


def parse_frame_timestamp(name: str) -> datetime | None:
    m = FRAME_RE.match(name)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)


def frame_disk_bytes(radar_id: str) -> int:
    total = 0
    fd = frames_dir(radar_id)
    if not fd.exists():
        return 0
    for p in fd.glob("*.png"):
        total += p.stat().st_size
    return total


def list_frames(
    radar_id: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return sorted frame records for a radar, optionally filtered by UTC range."""
    fd = frames_dir(radar_id)
    if not fd.exists():
        return []

    frames: list[dict[str, Any]] = []
    for path in sorted(fd.glob("*.png")):
        ts = parse_frame_timestamp(path.name)
        if ts is None:
            continue
        if start and ts < start:
            continue
        if end and ts > end:
            continue
        frames.append(
            {
                "filename": path.name,
                "path": str(path),
                "utc": ts.isoformat().replace("+00:00", "Z"),
                "timestamp": ts,
                "size": path.stat().st_size,
            }
        )
    return frames


def save_frame_if_new(
    radar_id: str,
    png_bytes: bytes,
    sha256: str,
    *,
    width: int,
    height: int,
    bbox_3857: list[float] | None,
    poll_interval_sec: float,
    product: str | None = None,
) -> tuple[Path | None, dict[str, Any], bool]:
    """
    Persist frame only if sha256 differs from last known.
    Returns (path_or_None, metadata, saved).
    """
    ensure_dirs()
    rid = radar_id.strip().upper()
    frames_dir(rid).mkdir(parents=True, exist_ok=True)
    meta = load_metadata(rid)
    prod = product or meta.get("product") or PRODUCT

    if meta.get("last_sha256") == sha256:
        meta["disk_bytes"] = frame_disk_bytes(rid)
        save_metadata(rid, meta)
        return None, meta, False

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d_%H%M%SZ")
    path = frames_dir(rid) / f"{stamp}.png"
    # Avoid rare same-second collisions.
    if path.exists():
        stamp = now.strftime("%Y%m%d_%H%M%S") + f"{now.microsecond:06d}Z"
        path = frames_dir(rid) / f"{stamp}.png"
        # Still colliding (extremely unlikely) — append counter.
        n = 1
        while path.exists():
            path = frames_dir(rid) / f"{stamp[:-1]}_{n}Z.png"
            n += 1

    path.write_bytes(png_bytes)
    meta.update(
        {
            "radar_id": rid,
            "product": prod,
            "last_frame_utc": now.isoformat().replace("+00:00", "Z"),
            "last_sha256": sha256,
            "frame_count": len(list(frames_dir(rid).glob("*.png"))),
            "width": width,
            "height": height,
            "poll_interval_sec": poll_interval_sec,
            "bbox_3857": bbox_3857,
            "disk_bytes": frame_disk_bytes(rid),
        }
    )
    save_metadata(rid, meta)
    return path, meta, True


def parse_iso_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
