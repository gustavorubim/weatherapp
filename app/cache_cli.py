from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from typing import Iterable

from app.cache_manager import manager, run_for_duration

logger = logging.getLogger(__name__)

_DURATION_RE = re.compile(
    r"^\s*(?:(?P<days>\d+(?:\.\d+)?)\s*d)?\s*"
    r"(?:(?P<hours>\d+(?:\.\d+)?)\s*h)?\s*"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)\s*m)?\s*"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)\s*s?)?\s*$",
    re.IGNORECASE,
)


def parse_duration(value: str) -> float:
    """
    Parse a human duration into seconds.

    Examples: 90, 90s, 30m, 2h, 2d, 1d12h, 48h
    """
    text = value.strip().lower()
    if not text:
        raise argparse.ArgumentTypeError("duration is empty")

    # Plain number => seconds
    try:
        seconds = float(text)
        if seconds < 0:
            raise argparse.ArgumentTypeError("duration must be >= 0")
        return seconds
    except ValueError:
        pass

    match = _DURATION_RE.match(text)
    if not match or not any(match.groups()):
        raise argparse.ArgumentTypeError(
            f"invalid duration {value!r}; try 90, 30m, 2h, 2d, or 1d12h"
        )

    days = float(match.group("days") or 0)
    hours = float(match.group("hours") or 0)
    minutes = float(match.group("minutes") or 0)
    secs = float(match.group("seconds") or 0)
    total = days * 86400 + hours * 3600 + minutes * 60 + secs
    if total <= 0:
        raise argparse.ArgumentTypeError("duration must be > 0")
    return total


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return "".join(parts)


async def run_many(radar_ids: Iterable[str], duration_sec: float, *, status_every: float) -> dict:
    ids = [r.strip().upper() for r in radar_ids]
    for rid in ids:
        manager.start(rid)
        logger.info("Started %s", rid)

    deadline = time.monotonic() + duration_sec
    logger.info(
        "Archiving %s for %s (Ctrl+C to stop early)",
        ", ".join(ids),
        format_duration(duration_sec),
    )
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(status_every, remaining))
            for rid in ids:
                st = manager.status_for(rid)
                logger.info(
                    "%s frames=%s running=%s last=%s err=%s",
                    rid,
                    st.get("frame_count"),
                    st.get("running"),
                    st.get("last_frame_utc"),
                    st.get("last_error"),
                )
    except asyncio.CancelledError:
        raise
    finally:
        for rid in ids:
            manager.stop(rid)
            logger.info("Stopped %s", rid)

    return {rid: manager.status_for(rid) for rid in ids}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="RadarVault cache CLI (no web UI required)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python -m app.cache_cli start KTBW --once
  python -m app.cache_cli start KTBW TMCO --for 2d
  python -m app.cache_cli start KTBW --for 48h --status-every 15m
  python -m app.cache_cli status
""",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    start = sub.add_parser("start", help="Archive one or more radars")
    start.add_argument("radar_ids", nargs="+", help="Radar IDs, e.g. KTBW TMCO")
    start.add_argument(
        "--for",
        dest="for_duration",
        type=parse_duration,
        default=None,
        metavar="DURATION",
        help="How long to run: 90, 30m, 2h, 2d, 1d12h (default: until Ctrl+C)",
    )
    start.add_argument(
        "--duration",
        type=parse_duration,
        default=None,
        help=argparse.SUPPRESS,  # alias kept for older docs/scripts
    )
    start.add_argument(
        "--status-every",
        type=parse_duration,
        default=parse_duration("5m"),
        metavar="DURATION",
        help="How often to log status while running (default: 5m)",
    )
    start.add_argument("--once", action="store_true", help="Fetch a single poll then exit")

    stop = sub.add_parser("stop", help="Stop caching a radar (same-process only)")
    stop.add_argument("radar_id")

    sub.add_parser("status", help="Show on-disk cache status")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.cmd == "start" and args.once:
        results = []
        for rid in args.radar_ids:
            results.append(manager.poll_once(rid))
        print(json.dumps(results if len(results) > 1 else results[0], indent=2, default=str))
        return 0

    if args.cmd == "start":
        duration = args.for_duration if args.for_duration is not None else args.duration
        ids = [r.strip().upper() for r in args.radar_ids]

        if duration and duration > 0:
            try:
                if len(ids) == 1 and args.status_every >= duration:
                    # Simple path for short one-radar runs
                    status = asyncio.run(run_for_duration(ids[0], duration))
                    print(json.dumps(status, indent=2))
                else:
                    status = asyncio.run(run_many(ids, duration, status_every=args.status_every))
                    print(json.dumps(status, indent=2))
            except KeyboardInterrupt:
                for rid in ids:
                    manager.stop(rid)
                print(json.dumps({rid: manager.status_for(rid) for rid in ids}, indent=2))
                return 130
            return 0

        # Until Ctrl+C
        for rid in ids:
            manager.start(rid)
        print(json.dumps({rid: manager.status_for(rid) for rid in ids}, indent=2))
        print(f"Caching {', '.join(ids)}… Ctrl+C to stop")
        try:
            while True:
                time.sleep(args.status_every)
                for rid in ids:
                    print(json.dumps(manager.status_for(rid), indent=2))
        except KeyboardInterrupt:
            for rid in ids:
                manager.stop(rid)
            print(json.dumps({rid: manager.status_for(rid) for rid in ids}, indent=2))
        return 0

    if args.cmd == "stop":
        print(json.dumps(manager.stop(args.radar_id), indent=2))
        return 0

    if args.cmd == "status":
        print(json.dumps(manager.status(), indent=2))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
