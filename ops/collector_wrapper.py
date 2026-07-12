"""Run the headless collector under an archive-directory process lock.

This small wrapper is the service entrypoint.  It forwards TERM/INT to the
child CLI, waits for graceful shutdown, and always releases the lock.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path

# Running ``python /absolute/path/ops/collector_wrapper.py`` does not put the
# checkout root on sys.path; service templates use that absolute invocation.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import CACHE_DIR  # noqa: E402
from app.process_lock import LockHeldError, ProcessLock  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run RadarVault cache CLI as a service")
    parser.add_argument("cache_args", nargs=argparse.REMAINDER, help="arguments for app.cache_cli")
    args = parser.parse_args(argv)
    if not args.cache_args:
        parser.error("provide cache CLI arguments, for example start KTBW")

    lock_path = Path(os.getenv("RADARVAULT_LOCK_PATH", str(CACHE_DIR)))
    try:
        lock = ProcessLock(lock_path)
        if not lock.acquire():
            print(f"lock is held: {lock.path}", file=sys.stderr)
            return 75
    except LockHeldError as exc:
        print(str(exc), file=sys.stderr)
        return 75

    child: subprocess.Popen[bytes] | None = None
    stopping = False

    def forward(signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    try:
        child = subprocess.Popen([sys.executable, "-m", "app.cache_cli", *args.cache_args])
        while child.poll() is None:
            try:
                child.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                continue
        return int(child.returncode or 0)
    finally:
        if child is not None and stopping and child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
