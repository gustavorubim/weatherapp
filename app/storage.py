from __future__ import annotations

import json
import hashlib
import mimetypes
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

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
        "last_source_sha256": None,
        "last_stored_sha256": None,
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
        width = detail.get("width")
        height = detail.get("height")
        if not width or not height:
            # Older metadata recorded the configured canvas size rather than
            # the actual image dimensions. Probe legacy frames so API/video
            # consumers never receive a misleading size.
            try:
                with Image.open(path) as image:
                    width, height = image.size
            except Exception:  # noqa: BLE001
                width = width or metadata.get("width")
                height = height or metadata.get("height")
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
                "width": width,
                "height": height,
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
    source_sha256: str | None = None,
    stored_sha256: str | None = None,
    extension: str = ".png",
    preview_bytes: bytes | None = None,
    preview_path: Path | str | None = None,
    preview_media_type: str = "image/webp",
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

    source_digest = source_sha256 or sha256
    stored_digest = stored_sha256 or hashlib.sha256(png_bytes).hexdigest()
    if meta.get("last_source_sha256") == source_digest or (
        not meta.get("last_source_sha256") and meta.get("last_sha256") == source_digest
    ):
        meta["disk_bytes"] = frame_disk_bytes(rid)
        save_metadata(rid, meta)
        return None, meta, False

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d_%H%M%SZ")
    extension = extension if extension.startswith(".") else f".{extension}"
    extension = extension.lower()
    if extension not in FRAME_EXTENSIONS:
        raise ValueError(f"unsupported frame extension: {extension}")
    path = frames_dir(rid) / f"{stamp}{extension}"
    # Avoid rare same-second collisions.
    if path.exists():
        stamp = now.strftime("%Y%m%d_%H%M%S") + f"{now.microsecond:06d}Z"
        path = frames_dir(rid) / f"{stamp}{extension}"
        # Still colliding (extremely unlikely) — append counter.
        n = 1
        while path.exists():
            path = frames_dir(rid) / f"{stamp[:-1]}_{n}Z{extension}"
            n += 1

    def _write_atomic(target: Path, payload: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    resolved_preview: Path | None = None
    if preview_bytes is not None:
        if preview_path:
            candidate = Path(preview_path)
            resolved_preview = (
                candidate / f"{path.stem}.webp" if candidate.suffix == "" else candidate
            )
        else:
            resolved_preview = radar_dir(rid) / "previews" / f"{path.stem}.webp"
    try:
        # Stage both artifacts before metadata becomes visible. Each replace
        # is atomic, and a preview failure rolls back the newly-created
        # archive so readers never observe a half-published frame pair.
        _write_atomic(path, png_bytes)
        if resolved_preview is not None:
            _write_atomic(resolved_preview, preview_bytes or b"")
    except Exception:
        for candidate in (path, resolved_preview):
            if candidate is not None and candidate.exists():
                try:
                    candidate.unlink()
                except OSError:
                    pass
        raise

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
        "source_sha256": source_digest,
        "stored_sha256": stored_digest,
        "preview_path": str(resolved_preview) if resolved_preview else None,
        "preview_media_type": preview_media_type if resolved_preview else None,
    }
    meta.update(
        {
            "radar_id": rid,
            "product": prod,
            "last_frame_utc": now.isoformat().replace("+00:00", "Z"),
            # Keep last_sha256 for backwards compatibility; it is the source
            # digest used for WMS deduplication.
            "last_sha256": source_digest,
            "last_source_sha256": source_digest,
            "last_stored_sha256": stored_digest,
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


def _preview_path_for_frame(radar_id: str, filename: str, detail: dict[str, Any] | None = None) -> Path | None:
    if detail and detail.get("preview_path"):
        return Path(str(detail["preview_path"]))
    stem = Path(filename).stem
    candidate = radar_dir(radar_id) / "previews" / f"{stem}.webp"
    return candidate if candidate.is_file() else None


def _refresh_metadata_after_delete(radar_id: str, meta: dict[str, Any], removed: set[str]) -> dict[str, Any]:
    """Drop deleted frame entries and recompute aggregate metadata fields."""
    rid = radar_id.strip().upper()
    frame_details = meta.get("frames")
    if isinstance(frame_details, dict):
        for name in removed:
            frame_details.pop(name, None)
    remaining = list_frames(rid, limit=5000)
    meta["frame_count"] = len(remaining)
    meta["disk_bytes"] = frame_disk_bytes(rid)
    if remaining:
        last = remaining[-1]
        detail = (frame_details or {}).get(last["filename"], {}) if isinstance(frame_details, dict) else {}
        meta["last_frame_utc"] = last.get("utc")
        meta["last_fetched_at"] = detail.get("fetched_at") or last.get("fetched_at")
        meta["last_observed_at"] = detail.get("observed_at") or last.get("observed_at")
        meta["last_source_sha256"] = detail.get("source_sha256") or detail.get("sha256") or meta.get("last_source_sha256")
        meta["last_stored_sha256"] = detail.get("stored_sha256") or detail.get("sha256") or meta.get("last_stored_sha256")
        meta["last_sha256"] = meta.get("last_source_sha256") or meta.get("last_sha256")
    else:
        meta["last_frame_utc"] = None
        meta["last_fetched_at"] = None
        meta["last_observed_at"] = None
        meta["last_source_sha256"] = None
        meta["last_stored_sha256"] = None
        meta["last_sha256"] = None
    save_metadata(rid, meta)
    return meta


def delete_frame(radar_id: str, filename: str) -> dict[str, Any]:
    """Delete one archived frame (and its preview) and refresh metadata."""
    rid = radar_id.strip().upper()
    if not filename or Path(filename).name != filename or ".." in filename:
        raise ValueError("Invalid filename")
    path = frames_dir(rid) / filename
    if not path.is_file():
        raise FileNotFoundError(f"Frame not found: {filename}")
    meta = load_metadata(rid)
    detail = (meta.get("frames") or {}).get(filename) if isinstance(meta.get("frames"), dict) else {}
    detail = detail if isinstance(detail, dict) else {}
    size = path.stat().st_size
    preview = _preview_path_for_frame(rid, filename, detail)
    path.unlink()
    if preview is not None and preview.is_file() and preview != path:
        try:
            preview.unlink()
        except OSError:
            pass
    _refresh_metadata_after_delete(rid, meta, {filename})
    return {
        "radar_id": rid,
        "deleted": [filename],
        "deleted_count": 1,
        "reclaimed_bytes": size,
    }


def delete_frames(
    radar_id: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    before: datetime | None = None,
    filenames: list[str] | None = None,
) -> dict[str, Any]:
    """Delete frames for a radar by filename list and/or time bounds.

    ``before`` is an exclusive upper bound (frames with timestamp < before).
    ``start``/``end`` are inclusive bounds matching ``list_frames``.
    """
    rid = radar_id.strip().upper()
    if filenames:
        targets = []
        for name in filenames:
            if not name or Path(name).name != name or ".." in name:
                raise ValueError(f"Invalid filename: {name}")
            path = frames_dir(rid) / name
            if path.is_file():
                ts = parse_frame_timestamp(name)
                targets.append(
                    {
                        "filename": name,
                        "path": str(path),
                        "size": path.stat().st_size,
                        "timestamp": ts,
                    }
                )
    else:
        end_bound = end
        if before is not None:
            # Exclusive before → inclusive end just under the cutoff via list filter.
            end_bound = before
        targets = list_frames(rid, start=start, end=end_bound, limit=5000)
        if before is not None:
            targets = [frame for frame in targets if frame["timestamp"] < before]

    if not targets:
        return {"radar_id": rid, "deleted": [], "deleted_count": 0, "reclaimed_bytes": 0}

    meta = load_metadata(rid)
    frame_details = meta.get("frames") if isinstance(meta.get("frames"), dict) else {}
    deleted: list[str] = []
    reclaimed = 0
    for frame in targets:
        filename = str(frame["filename"])
        path = Path(frame["path"])
        if not path.is_file():
            continue
        detail = frame_details.get(filename) if isinstance(frame_details, dict) else {}
        detail = detail if isinstance(detail, dict) else {}
        size = int(frame.get("size") or path.stat().st_size)
        preview = _preview_path_for_frame(rid, filename, detail)
        try:
            path.unlink()
        except OSError:
            continue
        if preview is not None and preview.is_file() and preview != path:
            try:
                preview.unlink()
            except OSError:
                pass
        deleted.append(filename)
        reclaimed += size

    if deleted:
        _refresh_metadata_after_delete(rid, meta, set(deleted))
    return {
        "radar_id": rid,
        "deleted": deleted,
        "deleted_count": len(deleted),
        "reclaimed_bytes": reclaimed,
    }


def delete_radar_cache(radar_id: str) -> dict[str, Any]:
    """Remove all frames (and previews) for a radar, keeping an empty archive shell."""
    rid = radar_id.strip().upper()
    result = delete_frames(rid, start=None, end=None)
    # Clear empty dirs of leftover preview files that had no matching frame entry.
    preview_dir = radar_dir(rid) / "previews"
    if preview_dir.is_dir():
        for path in preview_dir.iterdir():
            if path.is_file():
                try:
                    path.unlink()
                except OSError:
                    pass
    return result


def list_cached_radars() -> list[dict[str, Any]]:
    """Summarize each radar directory that has frames or metadata on disk."""
    ensure_dirs()
    summaries: list[dict[str, Any]] = []
    if not CACHE_DIR.exists():
        return summaries
    for path in sorted(CACHE_DIR.iterdir()):
        if not path.is_dir():
            continue
        rid = path.name.strip().upper()
        frames = list_frames(rid, limit=5000)
        meta = load_metadata(rid)
        first_utc = frames[0]["utc"] if frames else None
        last_utc = frames[-1]["utc"] if frames else None
        summaries.append(
            {
                "radar_id": rid,
                "frame_count": len(frames),
                "disk_bytes": meta.get("disk_bytes") or frame_disk_bytes(rid),
                "first_utc": first_utc,
                "last_utc": last_utc,
                "product": meta.get("product"),
            }
        )
    return summaries
