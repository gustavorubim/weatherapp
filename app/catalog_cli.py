"""Maintenance CLI for the RadarVault SQLite frame catalog.

The commands are intentionally filesystem-safe: ``rebuild`` is a dry run by
default, and ``retention`` only deletes when ``--apply`` is explicit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from PIL import Image

from app.catalog import Catalog, FrameRecord
from app.config import CACHE_DIR
from app.retention import RetentionPolicy, apply_retention, plan_retention
from app.storage import parse_frame_timestamp


def _iso_from_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_legacy_frames(cache_dir: Path) -> Iterable[tuple[str, Path]]:
    if not cache_dir.exists():
        return
    for radar_dir in sorted(cache_dir.iterdir()):
        frames_dir = radar_dir / "frames"
        if not radar_dir.is_dir() or not frames_dir.is_dir():
            continue
        for path in sorted(frames_dir.iterdir()):
            if path.is_file() and path.suffix.lower() in {".png", ".webp", ".jpg", ".jpeg"}:
                yield radar_dir.name.strip().upper(), path


def _record_for_file(radar_id: str, path: Path, *, product: str = "sr_bref") -> FrameRecord:
    stat = path.stat()
    digest = _sha256(path)
    try:
        with Image.open(path) as image:
            width, height = image.size
            media_type = Image.MIME.get(image.format or "", "application/octet-stream")
    except (OSError, ValueError):
        # A rebuild should report malformed files without inventing dimensions.
        raise ValueError(f"cannot decode image {path}") from None
    if not media_type or media_type == "application/octet-stream":
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    # Legacy names are fetch-time names, not source observation timestamps.
    # Keep observed_at unknown unless a future manifest provides it.
    fetched = parse_frame_timestamp(path.name)
    fetched_at = (
        fetched.isoformat().replace("+00:00", "Z")
        if fetched is not None
        else _iso_from_timestamp(stat.st_mtime)
    )
    preview = path.parent.parent / "previews" / f"{path.stem}.webp"
    return FrameRecord(
        radar_id=radar_id,
        filename=path.name,
        path=str(path.resolve()),
        preview_path=str(preview.resolve()) if preview.exists() else None,
        product=product,
        observed_at=None,
        fetched_at=fetched_at,
        width=int(width),
        height=int(height),
        media_type=media_type,
        source_sha256=digest,
        stored_sha256=digest,
        bytes=int(stat.st_size),
    )


def rebuild(cache_dir: str | Path, database: str | Path, *, dry_run: bool = True) -> dict:
    root = Path(cache_dir).expanduser().resolve()
    records: list[FrameRecord] = []
    errors: list[dict[str, str]] = []
    product_by_radar: dict[str, str] = {}
    for radar_dir in root.iterdir() if root.exists() else []:
        metadata = radar_dir / "metadata.json"
        if radar_dir.is_dir() and metadata.is_file():
            try:
                raw = json.loads(metadata.read_text())
                if isinstance(raw, dict) and raw.get("product"):
                    product_by_radar[radar_dir.name.strip().upper()] = str(raw["product"])
            except (OSError, ValueError, json.JSONDecodeError):
                pass
    for radar_id, path in _iter_legacy_frames(root):
        try:
            records.append(_record_for_file(radar_id, path, product=product_by_radar.get(radar_id, "sr_bref")))
        except (OSError, ValueError) as exc:
            errors.append({"path": str(path), "error": str(exc)})

    payload = {
        "cache_dir": str(root),
        "database": str(Path(database).expanduser()),
        "scanned": len(records) + len(errors),
        "records": len(records),
        "errors": errors,
        "dry_run": dry_run,
    }
    if not dry_run:
        with Catalog(database) as catalog:
            catalog.record_frames(records)
            payload["catalog_count"] = catalog.count()
    return payload


def _service_check(config_dir: str | Path) -> dict:
    root = Path(config_dir).expanduser()
    required = [root / "radarvault.launchd.plist", root / "radarvault.service"]
    checks: dict[str, bool] = {}
    errors: list[str] = []
    for path in required:
        exists = path.is_file()
        checks[path.name] = exists
        if not exists:
            errors.append(f"missing {path}")
            continue
        text = path.read_text()
        if "/ABSOLUTE/PATH/" not in text:
            errors.append(f"{path.name} must retain /ABSOLUTE/PATH/ placeholders")
        if "app.cache_cli" not in text:
            errors.append(f"{path.name} does not invoke app.cache_cli")
    return {"ok": not errors, "config": str(root), "checks": checks, "errors": errors}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RadarVault catalog and archive maintenance")
    sub = parser.add_subparsers(dest="command", required=True)

    rebuild_parser = sub.add_parser("rebuild", help="Index legacy cache frames")
    rebuild_parser.add_argument("--cache-dir", default=str(CACHE_DIR))
    rebuild_parser.add_argument("--database", default=str(CACHE_DIR / "catalog.sqlite3"))
    rebuild_parser.add_argument("--dry-run", action="store_true", default=False)

    verify_parser = sub.add_parser("verify", help="Run SQLite integrity checks")
    verify_parser.add_argument("--database", default=str(CACHE_DIR / "catalog.sqlite3"))

    retention_parser = sub.add_parser("retention", help="Plan or apply archive retention")
    retention_parser.add_argument("--database", default=str(CACHE_DIR / "catalog.sqlite3"))
    retention_parser.add_argument("--max-total-bytes", type=int)
    retention_parser.add_argument("--max-age-days", type=int)
    retention_parser.add_argument("--min-free-bytes", type=int)
    retention_parser.add_argument("--allow-pinned", action="store_true")
    retention_parser.add_argument("--apply", action="store_true")
    retention_parser.add_argument("--dry-run", action="store_true", help="Explicit no-delete mode")

    service_parser = sub.add_parser("service-check", help="Validate service templates")
    service_parser.add_argument("--config", default="ops")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "rebuild":
        result = rebuild(args.cache_dir, args.database, dry_run=args.dry_run)
        print(json.dumps(result, indent=2))
        return 0 if not result["errors"] else 2

    if args.command == "verify":
        with Catalog(args.database) as catalog:
            result = catalog.verify()
            result["frame_count"] = catalog.count()
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1

    if args.command == "retention":
        policy = RetentionPolicy(
            max_total_bytes=args.max_total_bytes,
            max_age_days=args.max_age_days,
            min_free_bytes=args.min_free_bytes,
            preserve_pinned=not args.allow_pinned,
        )
        with Catalog(args.database) as catalog:
            plan = plan_retention(catalog, policy)
            retention_result = apply_retention(plan, dry_run=not args.apply or args.dry_run)
            payload = {"plan": plan.as_dict(), "result": retention_result.as_dict()}
        print(json.dumps(payload, indent=2))
        return 0 if retention_result.ok else 1

    if args.command == "service-check":
        result = _service_check(args.config)
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
