from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from app.process_lock import LockHeldError, ProcessLock


def test_lock_contention_and_release(tmp_path: Path) -> None:
    first = ProcessLock(tmp_path)
    second = ProcessLock(tmp_path)
    assert first.acquire()
    assert not second.acquire()
    assert second.owner and second.owner["pid"] == os.getpid()
    first.release()
    assert second.acquire()
    second.release()


def test_dead_stale_lock_is_recovered(tmp_path: Path) -> None:
    path = tmp_path / ".radarvault.lock"
    path.write_text(json.dumps({"pid": 99999999, "hostname": os.uname().nodename, "started_at": "2020-01-01T00:00:00Z"}))
    lock = ProcessLock(tmp_path)
    assert lock.is_stale()
    assert lock.recover_stale()
    assert lock.acquire()
    lock.release()


def test_term_shutdown_subprocess_releases_lock(tmp_path: Path) -> None:
    code = (
        "import signal,sys,time\n"
        "from app.process_lock import ProcessLock\n"
        "lock=ProcessLock(sys.argv[1]); lock.acquire()\n"
        "def stop(signum, frame):\n"
        "    lock.release(); raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        assert (tmp_path / ".radarvault.lock").exists()
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=5) == 0
        assert not (tmp_path / ".radarvault.lock").exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_blocking_timeout_raises(tmp_path: Path) -> None:
    first = ProcessLock(tmp_path)
    second = ProcessLock(tmp_path)
    assert first.acquire()
    try:
        with pytest.raises(LockHeldError):
            second.acquire(blocking=True, timeout=0.05)
    finally:
        first.release()
