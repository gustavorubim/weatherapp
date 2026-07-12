from __future__ import annotations

import json
import mimetypes
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import CACHE_DIR, IMAGE_HEIGHT, IMAGE_WIDTH, POLL_INTERVAL_SEC, PRODUCT, ensure_dirs

FRAME_RE = re.compile(
    r"^(\d{8}_\d{6})(?:\d{6})?(?:_\d+)?Z\.(?P<extension>png|png8|webp)$",
    re.IGNORECASE,
)
FRAME_EXTENSIONS = {".png", ".png8", ".webp"}


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
        "last_observed_at": None,
        "last_fetched_at": None,
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
    # Metadata is replaced atomically so a process restart cannot leave a
    # truncated JSON document beside an otherwise valid archive.
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(meta, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


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
    for p in fd.iterdir():
        if p.is_file() and p.suffix.lower() in FRAME_EXTENSIONS:
            total += p.stat().st_size
    return total


def list_frames(
    radar_id: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    after: str | datetime | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Return a bounded, sorted page of frame records for a radar.

    ``after`` is an exclusive cursor and accepts either a frame filename or an
    ISO-8601 UTC timestamp. Legacy PNG archives remain the source of truth; a
    catalog can be layered on later without changing this API.
    """
    fd = frames_dir(radar_id)
    if not fd.exists():
        return []

    limit = max(1, min(int(limit), 5000))
    after_name: str | None = None
    after_dt: datetime | None = None
    if isinstance(after, datetime):
        after_dt = after.astimezone(timezone.utc)
    elif after:
        after_name = after.strip()
        after_dt = parse_frame_timestamp(after_name) or parse_iso_utc(after_name)

    frames: list[dict[str, Any]] = []
    metadata = load_metadata(radar_id)
    per_frame = metadata.get("frames") or {}
    for path in sorted(fd.iterdir(), key=lambda candidate: candidate.name):
        if not path.is_file() or path.suffix.lower() not in FRAME_EXTENSIONS:
            continue
        ts = parse_frame_timestamp(path.name)
        if ts is None:
            continue
        if after_dt and ts < after_dt:
            continue
        if after_dt and ts == after_dt and after_name and path.name <= after_name:
            continue
        if start and ts < start:
            continue
        if end and ts > end:
            continue
        detail = per_frame.get(path.name) if isinstance(per_frame, dict) else None
        detail = detail if isinstance(detail, dict) else {}
        fetched_at = detail.get("fetched_at")
        if not fetched_at:
            fetched_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
        media_type = detail.get("media_type") or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        frames.append(
            {
                "filename": path.name,
                "path": str(path),
                "utc": ts.isoformat().replace("+00:00", "Z"),
                "timestamp": ts,
                "size": path.stat().st_size,
                "preview_path": detail.get("preview_path"),
                "observed_at": detail.get("observed_at"),
                "fetched_at": fetched_at,
                "width": detail.get("width") or metadata.get("width"),
                "height": detail.get("height") or metadata.get("height"),
                "media_type": media_type,
                "source_sha256": detail.get("source_sha256") or detail.get("sha256"),
                "stored_sha256": detail.get("stored_sha256") or detail.get("sha256"),
            }
        )
        if len(frames) >= limit:
            break
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
    observed_at: str | datetime | None = None,
    fetched_at: str | datetime | None = None,
    media_type: str = "image/png",
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

    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(png_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)

    fetched = _normalise_timestamp(fetched_at) or datetime.now(timezone.utc)
    observed = _normalise_timestamp(observed_at)
    frame_details = meta.setdefault("frames", {})
    if not isinstance(frame_details, dict):
        frame_details = {}
        meta["frames"] = frame_details
    frame_details[path.name] = {
        "observed_at": observed.isoformat().replace("+00:00", "Z") if observed else None,
        "fetched_at": fetched.isoformat().replace("+00:00", "Z"),
        "width": width,
        "height": height,
        "media_type": media_type,
        "source_sha256": sha256,
        "stored_sha256": sha256,
    }
    meta.update(
        {
            "radar_id": rid,
            "product": prod,
            "last_frame_utc": now.isoformat().replace("+00:00", "Z"),
            "last_sha256": sha256,
            "frame_count": sum(
                1
                for candidate in frames_dir(rid).iterdir()
                if candidate.is_file() and candidate.suffix.lower() in FRAME_EXTENSIONS
            ),
            "width": width,
            "height": height,
            "poll_interval_sec": poll_interval_sec,
            "bbox_3857": bbox_3857,
            "disk_bytes": frame_disk_bytes(rid),
            "last_observed_at": observed.isoformat().replace("+00:00", "Z") if observed else None,
            "last_fetched_at": fetched.isoformat().replace("+00:00", "Z"),
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


def _normalise_timestamp(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return parse_iso_utc(value)
