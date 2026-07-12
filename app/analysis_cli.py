"""CLI for offline reflectivity analysis and experimental nowcasting."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from app.analysis.cells import detect_cells, track_cells, tracks_report
from app.analysis.clutter import build_clutter_frequency
from app.analysis.evaluation import evaluate_nowcast, split_by_time_blocks
from app.analysis.fixtures import synthetic_moving_cell
from app.analysis.motion import estimate_motion
from app.analysis.nowcast import SUPPORTED_LEAD_MINUTES, advect_nowcast, persistence_nowcast
from app.analysis.provenance import dumps_json, sha256_file
from app.analysis.reflectivity import decode_reflectivity_bins, paint_bins
from app.storage import list_frames, parse_iso_utc


def _load_cache_frames(
    radar_id: str,
    *,
    cache_dir: Path | None,
    start: datetime | None,
    end: datetime | None,
) -> list[dict[str, Any]]:
    if cache_dir is not None:
        # Temporary explicit path — WT7 owns env wiring.
        from app import storage as storage_mod

        original = storage_mod.CACHE_DIR
        try:
            storage_mod.CACHE_DIR = cache_dir
            return list_frames(radar_id, start=start, end=end)
        finally:
            storage_mod.CACHE_DIR = original
    return list_frames(radar_id, start=start, end=end)


def _cmd_cells(args: argparse.Namespace) -> int:
    start = parse_iso_utc(args.start)
    end = parse_iso_utc(args.end)
    frames = _load_cache_frames(
        args.radar_id,
        cache_dir=Path(args.cache_dir) if args.cache_dir else None,
        start=start,
        end=end,
    )
    report: dict[str, Any] = {
        "experimental": True,
        "radar_id": args.radar_id.upper(),
        "start": args.start,
        "end": args.end,
        "dry_run": bool(args.dry_run),
        "frame_count": len(frames),
        "disclaimer": (
            "Experimental reflectivity-only analysis. "
            "Not a severe-weather forecast."
        ),
    }
    if args.dry_run:
        report["would_analyze"] = [f["filename"] for f in frames]
        print(dumps_json(report))
        return 0

    timestamped = []
    overlays = []
    for fr in frames:
        path = Path(fr["path"])
        bins = decode_reflectivity_bins(path.read_bytes())
        assert not isinstance(bins, tuple)
        cells = detect_cells(bins, min_bin=args.min_bin, min_pixels=args.min_pixels)
        timestamped.append((fr["timestamp"], cells))
        if args.overlay_dir:
            overlay = paint_bins(bins)
            overlays.append((fr["filename"], overlay, cells))

    tracks = track_cells(
        timestamped,
        max_speed_kmh=args.max_speed_kmh,
        km_per_pixel=args.km_per_pixel,
    )
    report["tracks"] = tracks_report(tracks)
    out = Path(args.output) if args.output else None
    text = dumps_json(report)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        print(f"Wrote {out}")
    else:
        print(text)

    if args.overlay_dir and overlays:
        odir = Path(args.overlay_dir)
        odir.mkdir(parents=True, exist_ok=True)
        for name, img, cells in overlays:
            # Draw simple centroids as white pixels.
            px = img.copy()
            arr = __import__("numpy").asarray(px)
            for cell in cells:
                y, x = int(round(cell.centroid_yx[0])), int(round(cell.centroid_yx[1]))
                if 0 <= y < arr.shape[0] and 0 <= x < arr.shape[1]:
                    arr[max(0, y - 1) : y + 2, max(0, x - 1) : x + 2] = (255, 255, 255, 255)
            from PIL import Image

            Image.fromarray(arr).save(odir / f"cells_{name}")
        print(f"Wrote {len(overlays)} overlays to {odir}")
    return 0


def _cmd_clutter(args: argparse.Namespace) -> int:
    start = parse_iso_utc(args.start)
    end = parse_iso_utc(args.end)
    frames = _load_cache_frames(
        args.radar_id,
        cache_dir=Path(args.cache_dir) if args.cache_dir else None,
        start=start,
        end=end,
    )
    if args.dry_run:
        print(
            dumps_json(
                {
                    "experimental": True,
                    "dry_run": True,
                    "frame_count": len(frames),
                    "would_analyze": [f["filename"] for f in frames],
                }
            )
        )
        return 0
    paths = [{"path": f["path"]} for f in frames]
    if not paths:
        print(dumps_json({"error": "no frames", "frame_count": 0}))
        return 1
    result = build_clutter_frequency(paths, min_presence=args.min_presence, min_bin=args.min_bin)
    payload = {
        "metrics": result.metrics,
        "provenance": result.provenance,
        "experimental": True,
    }
    text = dumps_json(payload)
    if args.output:
        Path(args.output).write_text(text)
        print(f"Wrote {args.output}")
    else:
        print(text)
    if args.overlay_dir and result.mask_rgba is not None:
        from PIL import Image

        odir = Path(args.overlay_dir)
        odir.mkdir(parents=True, exist_ok=True)
        Image.fromarray(result.mask_rgba).save(odir / "clutter_mask.png")
        print(f"Wrote clutter mask to {odir / 'clutter_mask.png'}")
    return 0


def _cmd_nowcast(args: argparse.Namespace) -> int:
    start = parse_iso_utc(args.start) if args.start else None
    end = parse_iso_utc(args.end) if args.end else None
    frames = _load_cache_frames(
        args.radar_id,
        cache_dir=Path(args.cache_dir) if args.cache_dir else None,
        start=start,
        end=end,
    )
    lead = int(args.lead_minutes)
    if lead not in SUPPORTED_LEAD_MINUTES:
        print(
            f"lead-minutes must be one of {SUPPORTED_LEAD_MINUTES}",
            file=sys.stderr,
        )
        return 2
    if args.dry_run:
        print(
            dumps_json(
                {
                    "experimental": True,
                    "dry_run": True,
                    "radar_id": args.radar_id.upper(),
                    "lead_minutes": lead,
                    "frame_count": len(frames),
                    "disclaimer": (
                        "Experimental reflectivity-only nowcast. "
                        "Does not claim severe-weather prediction."
                    ),
                    "would_use": [f["filename"] for f in frames[-8:]],
                }
            )
        )
        return 0
    if len(frames) < 2:
        print(dumps_json({"error": "need at least 2 frames", "frame_count": len(frames)}))
        return 1

    use = frames[-8:]
    bin_frames = []
    timestamps = []
    for fr in use:
        data = Path(fr["path"]).read_bytes()
        bins = decode_reflectivity_bins(data)
        assert not isinstance(bins, tuple)
        bin_frames.append({"bins": bins, "source_hash": sha256_file(fr["path"])})
        timestamps.append(fr["timestamp"])

    motion = estimate_motion(bin_frames, timestamps)
    result = advect_nowcast(bin_frames[-1], motion, lead_minutes=lead)
    baseline = persistence_nowcast(bin_frames[-1], lead_minutes=lead)
    payload = {
        "experimental": True,
        "lead_minutes": lead,
        "method": result.method,
        "provenance": result.provenance,
        "persistence_provenance": baseline.provenance,
        "motion": {
            "mean_u_px_per_hour": motion.mean_u_px_per_hour,
            "mean_v_px_per_hour": motion.mean_v_px_per_hour,
            "gap_flags": list(motion.gap_flags),
        },
        "disclaimer": result.provenance.get("disclaimer"),
    }
    text = dumps_json(payload)
    if args.output:
        Path(args.output).write_text(text)
        print(f"Wrote {args.output}")
    else:
        print(text)
    if args.overlay_dir:
        from PIL import Image

        odir = Path(args.overlay_dir)
        odir.mkdir(parents=True, exist_ok=True)
        paint_bins(result.prediction).save(odir / f"nowcast_{lead}min.png")
        paint_bins(baseline.prediction).save(odir / f"persistence_{lead}min.png")
        print(f"Wrote nowcast overlays to {odir}")
    return 0


def _cmd_evaluate(args: argparse.Namespace) -> int:
    if args.fixture != "synthetic-moving-cell":
        print(f"Unknown fixture: {args.fixture}", file=sys.stderr)
        return 2
    fixture = synthetic_moving_cell()
    frames = fixture["frames_bins"]
    timestamps = fixture["timestamps"]
    hashes = fixture["source_hashes"]
    lead = int(fixture["documented_lead_minutes"])

    # Use frames[:-2] for motion, predict from frames[-2] to match frames[-1].
    motion = estimate_motion(frames[:-1], timestamps[:-1])
    adv = advect_nowcast(frames[-2], motion, lead_minutes=lead)
    pers = persistence_nowcast(frames[-2], lead_minutes=lead)
    obs = frames[-1]

    adv_eval = evaluate_nowcast(adv.prediction, obs, thresholds=(fixture["frames_bins"][0].max(),))
    # Use the cell bin as threshold
    thr = int(frames[0][frames[0] >= 0].max()) if (frames[0] >= 0).any() else 1
    adv_eval = evaluate_nowcast(adv.prediction, obs, thresholds=(thr,))
    pers_eval = evaluate_nowcast(pers.prediction, obs, thresholds=(thr,))

    splits = split_by_time_blocks(
        timestamps,
        hashes,
        block_minutes=1000.0,  # single contiguous → chronological half split
        tune_ratio=0.4,
    )

    adv_csi = adv_eval["thresholds"][0]["csi"]
    pers_csi = pers_eval["thresholds"][0]["csi"]
    payload = {
        "fixture": fixture["name"],
        "experimental": True,
        "documented_lead_minutes": lead,
        "advection": adv_eval,
        "persistence": pers_eval,
        "advection_outperforms_persistence": adv_csi > pers_csi,
        "splits": {k: {"indices": list(v.indices), "n": len(v.indices)} for k, v in splits.items()},
        "notes": fixture["notes"],
        "disclaimer": (
            "Experimental reflectivity-only evaluation. "
            "No performance threshold is claimed on real KTBW samples."
        ),
    }
    print(dumps_json(payload))
    if not payload["advection_outperforms_persistence"]:
        print("Acceptance failed: advection did not outperform persistence", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app.analysis_cli",
        description="RadarVault offline analysis (experimental, reflectivity-only)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("radar_id", nargs="?", default="KTBW")
        sp.add_argument("--start", default=None)
        sp.add_argument("--end", default=None)
        sp.add_argument("--cache-dir", default=None, help="Explicit cache root (WT7 wires env)")
        sp.add_argument("--dry-run", action="store_true")
        sp.add_argument("--output", default=None)
        sp.add_argument("--overlay-dir", default=None)

    cells = sub.add_parser("cells", help="Detect and track cells over a time range")
    add_common(cells)
    cells.add_argument("--min-bin", type=int, default=4)
    cells.add_argument("--min-pixels", type=int, default=20)
    cells.add_argument("--max-speed-kmh", type=float, default=120.0)
    cells.add_argument("--km-per-pixel", type=float, default=0.225)
    cells.set_defaults(func=_cmd_cells)

    clutter = sub.add_parser("clutter", help="Build clutter frequency mask")
    add_common(clutter)
    clutter.add_argument("--min-presence", type=float, default=0.8)
    clutter.add_argument("--min-bin", type=int, default=0)
    clutter.set_defaults(func=_cmd_clutter)

    nowcast = sub.add_parser("nowcast", help="Experimental advection nowcast")
    add_common(nowcast)
    nowcast.add_argument("--lead-minutes", type=int, default=15)
    nowcast.set_defaults(func=_cmd_nowcast)

    evaluate = sub.add_parser("evaluate", help="Evaluate nowcast on a fixture")
    evaluate.add_argument("--fixture", default="synthetic-moving-cell")
    evaluate.set_defaults(func=_cmd_evaluate)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
