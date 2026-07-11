from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.video import VideoError, export_video


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RadarVault video CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    export = sub.add_parser("export", help="Export MP4 from cached frames")
    export.add_argument("radar_id")
    export.add_argument("--start", default="2020-01-01")
    export.add_argument("--end", default="2099-01-01")
    export.add_argument("--fps", type=float, default=15)
    export.add_argument("--out", type=Path, default=None)
    export.add_argument("--crf", type=int, default=18)

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
            )
        except VideoError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps({"ok": True, "path": str(path), "bytes": path.stat().st_size}))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
