from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.video import QUALITY_PRESETS, VideoError, export_video


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="RadarVault video CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
quality presets:
  archive   CRF 15, slow   — highest quality
  balanced  CRF 18, medium — default
  small     CRF 26, fast   — smaller files

dimension policy:
  error      refuse mixed frame sizes (default)
  normalize  scale/pad to target (or dominant) size
""",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    export = sub.add_parser("export", help="Export MP4 from cached frames")
    export.add_argument("radar_id")
    export.add_argument("--start", default="2020-01-01")
    export.add_argument("--end", default="2099-01-01")
    export.add_argument("--fps", type=float, default=15)
    export.add_argument("--out", type=Path, default=None)
    export.add_argument("--crf", type=int, default=None, help="Override preset CRF")
    export.add_argument(
        "--quality",
        choices=sorted(QUALITY_PRESETS),
        default="balanced",
        help="Encode preset (default: balanced)",
    )
    export.add_argument(
        "--dimension-policy",
        choices=["error", "normalize"],
        default="error",
        help="How to handle mixed frame dimensions",
    )
    export.add_argument("--target-width", type=int, default=None)
    export.add_argument("--target-height", type=int, default=None)
    export.add_argument(
        "--timestamp-overlay",
        action="store_true",
        help="Burn a UTC range label onto the video",
    )

    args = parser.parse_args(argv)
    if args.cmd == "export":
        try:
            path = export_video(
                args.radar_id,
                start=args.start,
                end=args.end,
                fps=args.fps,
                out=args.out,
                crf=args.crf,
                quality=args.quality,
                dimension_policy=args.dimension_policy,
                target_width=args.target_width,
                target_height=args.target_height,
                timestamp_overlay=args.timestamp_overlay,
            )
        except VideoError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "ok": True,
                    "path": str(path),
                    "bytes": path.stat().st_size,
                    "quality": args.quality,
                    "dimension_policy": args.dimension_policy,
                    "output_size": list(getattr(export_video, "last_output_size", (0, 0))),
                    "temp_overhead_bytes": getattr(export_video, "last_temp_overhead_bytes", 0),
                    "source_bytes": getattr(export_video, "last_source_bytes", 0),
                }
            )
        )
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
