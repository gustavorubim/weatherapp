"""Cross-process ownership lock for an archive directory.

The lock uses an exclusive metadata file instead of an in-process mutex, so a
second CLI invocation cannot accidentally run a duplicate collector.  Owner
metadata makes a crashed process diagnosable and allows conservative stale
lock recovery.
"""

from __future__ import annotations

import json
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class LockHeldError(RuntimeError):
    """Raised when a caller requests an exclusive lock that is still held."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError, OSError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class ProcessLock:
    """Own a lockfile until :meth:`release` or context exit.

    ``path`` may be a directory (``.radarvault.lock`` is appended) or an
    explicit lockfile path.  ``stale_after_sec`` applies to malformed or remote
    owner metadata; local live PIDs are never considered stale solely due to
    age.  A dead local PID is stale immediately.
    """

    def __init__(self, path: str | Path, *, stale_after_sec: float = 3600.0) -> None:
        supplied = Path(path).expanduser()
        # Existing directories (including mktemp names with a dotted suffix)
        # are cache roots; explicit non-directory paths are lockfiles.
        self.path = supplied / ".radarvault.lock" if supplied.is_dir() or supplied.suffix == "" else supplied
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if stale_after_sec < 0:
            raise ValueError("stale_after_sec must be >= 0")
        self.stale_after_sec = float(stale_after_sec)
        self._metadata: dict[str, Any] | None = None
        self._owned = False

    @property
    def held(self) -> bool:
        return self._owned

    @property
    def owner(self) -> dict[str, Any] | None:
        return dict(self._metadata) if self._metadata else self.read_owner()

    def read_owner(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def is_stale(self, owner: dict[str, Any] | None = None) -> bool:
        owner = owner if owner is not None else self.read_owner()
        if not owner:
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                return False
            return age >= self.stale_after_sec
        host = str(owner.get("hostname", ""))
        pid = int(owner.get("pid", 0) or 0)
        if host == socket.gethostname() and pid:
            return not _pid_alive(pid)
        started = _parse_iso(str(owner.get("started_at", "")))
        return started is not None and time.time() - started >= self.stale_after_sec

    def recover_stale(self) -> bool:
        """Remove a stale lock, returning whether anything was recovered."""
        owner = self.read_owner()
        if not self.path.exists() or not self.is_stale(owner):
            return False
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        return True

    def acquire(
        self,
        *,
        blocking: bool = False,
        timeout: float | None = None,
        recover_stale: bool = True,
    ) -> bool:
        """Try to acquire the lock.

        ``blocking=False`` returns ``False`` when another live owner holds the
        lock.  ``blocking=True`` retries until ``timeout`` and then raises
        :class:`LockHeldError`; ``timeout=None`` waits indefinitely.
        """
        if self._owned:
            return True
        started = time.monotonic()
        while True:
            metadata = {
                "pid": os.getpid(),
                "hostname": socket.gethostname(),
                "started_at": _now_iso(),
                "lock_path": str(self.path),
            }
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                with os.fdopen(fd, "w") as stream:
                    json.dump(metadata, stream, sort_keys=True)
                    stream.write("\n")
                    stream.flush()
                self._metadata = metadata
                self._owned = True
                return True
            except FileExistsError:
                if recover_stale and self.recover_stale():
                    continue
                if not blocking:
                    return False
                if timeout is not None and time.monotonic() - started >= timeout:
                    raise LockHeldError(f"lock is held: {self.path}")
                time.sleep(0.05)

    def release(self) -> None:
        """Release only a lock owned by this instance."""
        if not self._owned:
            return
        try:
            owner = self.read_owner()
            metadata = self._metadata or {}
            if owner and owner.get("pid") == metadata.get("pid") and owner.get(
                "hostname"
            ) == metadata.get("hostname"):
                self.path.unlink(missing_ok=True)
        finally:
            self._metadata = None
            self._owned = False

    def __enter__(self) -> "ProcessLock":
        if not self.acquire():
            raise LockHeldError(f"lock is held: {self.path}")
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


__all__ = ["LockHeldError", "ProcessLock"]
