"""Filesystem capacity checks used by unattended collection."""

from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class DiskStatus:
    state: str
    path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    warning_threshold: int
    critical_threshold: int

    @property
    def ok(self) -> bool:
        return self.state == "ok"

    def as_dict(self) -> dict[str, int | str | bool]:
        data = asdict(self)
        data["ok"] = self.ok
        return data


class DiskGuard:
    """Classify free space as ``ok``, ``warning`` or ``critical``.

    Explicit byte thresholds take precedence over ratios.  This makes tests
    and deployment policy deterministic while retaining useful defaults for a
    small local archive.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        warning_free_bytes: int | None = None,
        critical_free_bytes: int | None = None,
        warning_free_ratio: float = 0.15,
        critical_free_ratio: float = 0.05,
    ) -> None:
        if warning_free_bytes is not None and warning_free_bytes < 0:
            raise ValueError("warning_free_bytes must be >= 0")
        if critical_free_bytes is not None and critical_free_bytes < 0:
            raise ValueError("critical_free_bytes must be >= 0")
        if warning_free_ratio < 0 or critical_free_ratio < 0:
            raise ValueError("free-space ratios must be >= 0")
        if warning_free_ratio > 1 or critical_free_ratio > 1:
            raise ValueError("free-space ratios must be <= 1")
        if critical_free_ratio > warning_free_ratio:
            raise ValueError("critical threshold must not exceed warning threshold")
        if (
            warning_free_bytes is not None
            and critical_free_bytes is not None
            and critical_free_bytes > warning_free_bytes
        ):
            raise ValueError("critical byte threshold must not exceed warning threshold")
        self.path = Path(path)
        self.warning_free_bytes = warning_free_bytes
        self.critical_free_bytes = critical_free_bytes
        self.warning_free_ratio = warning_free_ratio
        self.critical_free_ratio = critical_free_ratio

    def check(self) -> DiskStatus:
        target = self.path if self.path.exists() else self.path.parent
        usage = shutil.disk_usage(target)
        warning = (
            int(self.warning_free_bytes)
            if self.warning_free_bytes is not None
            else int(usage.total * self.warning_free_ratio)
        )
        critical = (
            int(self.critical_free_bytes)
            if self.critical_free_bytes is not None
            else int(usage.total * self.critical_free_ratio)
        )
        if usage.free <= critical:
            state = "critical"
        elif usage.free <= warning:
            state = "warning"
        else:
            state = "ok"
        return DiskStatus(
            state=state,
            path=str(target),
            total_bytes=int(usage.total),
            used_bytes=int(usage.used),
            free_bytes=int(usage.free),
            warning_threshold=warning,
            critical_threshold=critical,
        )

    def allow_write(self, required_bytes: int = 0) -> bool:
        """Return false for critical space or an insufficient write budget."""
        status = self.check()
        return status.state != "critical" and status.free_bytes - max(0, required_bytes) > status.critical_threshold


def disk_status(
    path: str | Path,
    *,
    warning_free_bytes: int | None = None,
    critical_free_bytes: int | None = None,
    warning_free_ratio: float = 0.15,
    critical_free_ratio: float = 0.05,
) -> DiskStatus:
    return DiskGuard(
        path,
        warning_free_bytes=warning_free_bytes,
        critical_free_bytes=critical_free_bytes,
        warning_free_ratio=warning_free_ratio,
        critical_free_ratio=critical_free_ratio,
    ).check()


__all__ = ["DiskGuard", "DiskStatus", "disk_status"]
