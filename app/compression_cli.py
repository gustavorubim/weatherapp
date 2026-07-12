"""Dry-run-first archive compression and preview generation CLI.

Examples::

    python -m app.compression_cli benchmark cache/KTBW/frames --limit 5
    python -m app.compression_cli generate-previews cache/KTBW/frames --apply
    python -m app.compression_cli convert cache/KTBW/frames --format webp-lossless --apply

The CLI never rewrites source frames in place.  Conversion outputs go to a
separate directory by default, and source deletion requires ``--delete-source``
in addition to ``--apply``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

from app.frame_codec import (
    EncodedFrame,
    FrameCodecError,
    encode_archive_frame,
    encode_preview_frame,
    probe_image,
)


IMAGE_SUFFIXES = {".png", ".webp", ".jpg", ".jpeg"}


def _source_files(root: Path, limit: int | None) -> list[Path]:
    if root.is_file():
        candidates = [root] if root.suffix.lower() in IMAGE_SUFFIXES else []
    elif root.is_dir():
        candidates = sorted(
            p
            for p in root.rglob("*")
            if p.is_file()
            and p.suffix.lower() in IMAGE_SUFFIXES
            # Derived output directories are intentionally ignored so a
            # second run remains idempotent when using the default paths.
            and not any(
                part == "previews" or part.startswith("converted-")
                for part in p.relative_to(root).parts[:-1]
            )
        )
    else:
        raise FileNotFoundError(root)
    return candidates if limit is None else candidates[: max(limit, 0)]


def _relative_name(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return path.name


def _default_output(root: Path, kind: str) -> Path:
    if root.is_file():
        parent = root.parent
    else:
        parent = root
    return parent / kind


def _atomic_write(path: Path, data: bytes) -> None:
    """Write bytes durably and atomically, leaving no partial destination."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _record(
    source: Path,
    output: Path | None,
    encoded: EncodedFrame,
    *,
    applied: bool,
    source_bytes: int | None = None,
) -> dict:
    source_size = source_bytes if source_bytes is not None else source.stat().st_size
    savings = source_size - len(encoded.data)
    return {
        "source_path": str(source),
        "output_path": str(output) if output else None,
        "source_sha256": encoded.source_sha256,
        "stored_sha256": encoded.stored_sha256,
        "source_bytes": source_size,
        "stored_bytes": len(encoded.data),
        "bytes_saved": savings,
        "reduction_ratio": round((savings / source_size), 6)
        if source_size
        else 0.0,
        "width": encoded.width,
        "height": encoded.height,
        "extension": encoded.extension,
        "media_type": encoded.media_type,
        "applied": applied,
    }


def _write_manifest(path: Path, *, command: str, records: list[dict], applied: bool) -> None:
    payload = {
        "schema": 1,
        "command": command,
        "applied": applied,
        "records": records,
        "source_count": len(records),
        "source_bytes": sum(r["source_bytes"] for r in records),
        "stored_bytes": sum(r["stored_bytes"] for r in records),
    }
    _atomic_write(path, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode())


def _emit(command: str, records: list[dict], *, applied: bool, manifest: Path | None) -> int:
    payload = {
        "ok": True,
        "command": command,
        "applied": applied,
        "source_count": len(records),
        "source_bytes": sum(r["source_bytes"] for r in records),
        "stored_bytes": sum(r["stored_bytes"] for r in records),
        "bytes_saved": sum(r["bytes_saved"] for r in records),
        "records": records,
    }
    if manifest and applied:
        _write_manifest(manifest, command=command, records=records, applied=applied)
        payload["manifest"] = str(manifest)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def benchmark(root: Path, *, limit: int | None, archive_format: str) -> int:
    records: list[dict] = []
    for source in _source_files(root, limit):
        encoded = encode_archive_frame(source.read_bytes(), archive_format=archive_format)
        records.append(_record(source, None, encoded, applied=False))
    return _emit("benchmark", records, applied=False, manifest=None)


def generate_previews(
    root: Path,
    *,
    limit: int | None,
    output_dir: Path | None,
    max_dimension: int,
    quality: int,
    apply: bool,
    manifest: Path | None,
) -> int:
    files = _source_files(root, limit)
    output_root = output_dir or _default_output(root, "previews")
    records: list[dict] = []
    for source in files:
        encoded = encode_preview_frame(
            source.read_bytes(), max_dimension=max_dimension, quality=quality
        )
        relative = _relative_name(source, root if root.is_dir() else source.parent)
        destination = output_root / Path(relative).with_suffix(encoded.extension)
        # A second run is a no-op at the bytes/content level and safely replaces
        # only the derived preview, never the source frame.
        if apply:
            _atomic_write(destination, encoded.data)
        records.append(_record(source, destination, encoded, applied=apply))
    return _emit("generate-previews", records, applied=apply, manifest=manifest)


def convert(
    root: Path,
    *,
    limit: int | None,
    archive_format: str,
    output_dir: Path | None,
    apply: bool,
    delete_source: bool,
    manifest: Path | None,
) -> int:
    if delete_source and not apply:
        raise FrameCodecError("--delete-source requires --apply")
    files = _source_files(root, limit)
    output_root = output_dir or _default_output(root, f"converted-{archive_format}")
    records: list[dict] = []
    for source in files:
        source_bytes = source.read_bytes()
        source_size = len(source_bytes)
        encoded = encode_archive_frame(source_bytes, archive_format=archive_format)
        relative = _relative_name(source, root if root.is_dir() else source.parent)
        destination = output_root / Path(relative).with_suffix(encoded.extension)
        if apply:
            # Write/verify/rename first.  The original is only removed after a
            # complete decode and hash check of the newly materialized output.
            _atomic_write(destination, encoded.data)
            written = destination.read_bytes()
            info = probe_image(written)
            if (info["width"], info["height"]) != (encoded.width, encoded.height):
                raise FrameCodecError(f"verification failed for {destination}: dimensions changed")
            if hashlib.sha256(written).hexdigest() != encoded.stored_sha256:
                raise FrameCodecError(f"verification failed for {destination}: stored hash changed")
            if delete_source:
                source.unlink()
        records.append(
            _record(source, destination, encoded, applied=apply, source_bytes=source_size)
        )
    return _emit("convert", records, applied=apply, manifest=manifest)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RadarVault dry-run-first frame compression tools")
    sub = parser.add_subparsers(dest="command", required=True)

    bench = sub.add_parser("benchmark", help="measure archive codec sizes without writing")
    bench.add_argument("root", type=Path)
    bench.add_argument("--limit", type=int, default=None)
    bench.add_argument("--format", dest="archive_format", default="webp-lossless", choices=["png", "png8", "webp-lossless"])
    bench.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)

    preview = sub.add_parser("generate-previews", help="create lightweight WebP previews")
    preview.add_argument("root", type=Path)
    preview.add_argument("--limit", type=int, default=None)
    preview.add_argument("--output-dir", type=Path, default=None)
    preview.add_argument("--max-dimension", type=int, default=768)
    preview.add_argument("--quality", type=int, default=82)
    preview.add_argument("--manifest", type=Path, default=None)
    preview.add_argument("--apply", action="store_true", help="write previews (default is dry-run)")
    preview.add_argument("--dry-run", action="store_true", help="show planned work without writing (default)")

    conversion = sub.add_parser("convert", help="convert archive frames to another codec")
    conversion.add_argument("root", type=Path)
    conversion.add_argument("--format", dest="archive_format", required=True, choices=["png", "png8", "webp-lossless"])
    conversion.add_argument("--limit", type=int, default=None)
    conversion.add_argument("--output-dir", type=Path, default=None)
    conversion.add_argument("--manifest", type=Path, default=None)
    conversion.add_argument("--apply", action="store_true", help="write converted frames (default is dry-run)")
    conversion.add_argument("--dry-run", action="store_true", help="show planned work without writing (default)")
    conversion.add_argument("--delete-source", action="store_true", help="delete source only after verified conversion")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "benchmark":
            return benchmark(args.root, limit=args.limit, archive_format=args.archive_format)
        if args.command == "generate-previews":
            return generate_previews(
                args.root,
                limit=args.limit,
                output_dir=args.output_dir,
                max_dimension=args.max_dimension,
                quality=args.quality,
                apply=args.apply and not args.dry_run,
                manifest=args.manifest,
            )
        if args.command == "convert":
            return convert(
                args.root,
                limit=args.limit,
                archive_format=args.archive_format,
                output_dir=args.output_dir,
                apply=args.apply and not args.dry_run,
                delete_source=args.delete_source,
                manifest=args.manifest,
            )
    except (FileNotFoundError, FrameCodecError, OSError) as exc:
        print(f"compression error: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
