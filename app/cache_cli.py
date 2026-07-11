from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from app.cache_manager import manager


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RadarVault cache CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    start = sub.add_parser("start", help="Start caching a radar")
    start.add_argument("radar_id")
    start.add_argument("--duration", type=float, default=0, help="Stop after N seconds (0=until Ctrl+C)")
    start.add_argument("--once", action="store_true", help="Fetch a single poll then exit")

    stop = sub.add_parser("stop", help="Stop caching a radar")
    stop.add_argument("radar_id")

    sub.add_parser("status", help="Show cache status")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.cmd == "once" or (args.cmd == "start" and args.once):
        result = manager.poll_once(args.radar_id)
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.cmd == "start":
        if args.duration and args.duration > 0:
            status = asyncio.run(manager_run(args.radar_id, args.duration))
            print(json.dumps(status, indent=2))
            return 0
        status = manager.start(args.radar_id)
        print(json.dumps(status, indent=2))
        print("Caching… Ctrl+C to stop")
        try:
            while True:
                asyncio.run(asyncio.sleep(5))
                print(json.dumps(manager.status_for(args.radar_id), indent=2))
        except KeyboardInterrupt:
            manager.stop(args.radar_id)
            print(json.dumps(manager.status_for(args.radar_id), indent=2))
        return 0

    if args.cmd == "stop":
        print(json.dumps(manager.stop(args.radar_id), indent=2))
        return 0

    if args.cmd == "status":
        print(json.dumps(manager.status(), indent=2))
        return 0

    return 1


async def manager_run(radar_id: str, duration: float):
    from app.cache_manager import run_for_duration

    return await run_for_duration(radar_id, duration)


if __name__ == "__main__":
    sys.exit(main())
