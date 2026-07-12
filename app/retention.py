"""Dry-run-first retention planning for catalogued frame archives."""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.catalog import Catalog, FrameRecord


@dataclass(frozen=True)
class RetentionPolicy:
    max_total_bytes: int | None = None
    max_age_days: int | None = None
    min_free_bytes: int | None = None
    preserve_pinned: bool = True

    def __post_init__(self) -> None:
        for name in ("max_total_bytes", "max_age_days", "min_free_bytes"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be >= 0")


@dataclass
class RetentionPlan:
    catalog: Catalog
    policy: RetentionPolicy
    candidates: list[FrameRecord] = field(default_factory=list)
    estimated_bytes: int = 0
    created_at: str = ""
    reasons: dict[str, list[str]] = field(default_factory=dict)
    pinned_bytes_over_quota: int = 0

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = _utc_now()

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)

    def as_dict(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "candidate_count": self.candidate_count,
            "estimated_bytes": self.estimated_bytes,
            "policy": {
                "max_total_bytes": self.policy.max_total_bytes,
                "max_age_days": self.policy.max_age_days,
                "min_free_bytes": self.policy.min_free_bytes,
                "preserve_pinned": self.policy.preserve_pinned,
            },
            "candidates": [
                {
                    "radar_id": frame.radar_id,
                    "filename": frame.filename,
                    "path": frame.path,
                    "bytes": frame.bytes,
                    "pinned": frame.pinned,
                    "reasons": self.reasons.get(_key(frame), []),
                }
                for frame in self.candidates
            ],
            "pinned_bytes_over_quota": self.pinned_bytes_over_quota,
        }


@dataclass
class RetentionResult:
    dry_run: bool
    planned: int
    deleted: list[str] = field(default_factory=list)
    reclaimed_bytes: int = 0
    failed: list[dict[str, Any]] = field(default_factory=list)
    skipped_pinned: int = 0

    @property
    def ok(self) -> bool:
        return not self.failed

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "planned": self.planned,
            "deleted": self.deleted,
            "reclaimed_bytes": self.reclaimed_bytes,
            "failed": self.failed,
            "skipped_pinned": self.skipped_pinned,
            "ok": self.ok,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime | None:
    try:
        text = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _effective_time(record: FrameRecord) -> datetime:
    return _parse(record.observed_at or record.fetched_at) or datetime.min.replace(
        tzinfo=timezone.utc
    )


def _key(record: FrameRecord) -> str:
    return f"{record.radar_id}/{record.filename}"


def _add_candidate(
    candidates: dict[tuple[str, str], FrameRecord],
    reasons: dict[str, list[str]],
    record: FrameRecord,
    reason: str,
    *,
    preserve_pinned: bool,
) -> None:
    if preserve_pinned and record.pinned:
        return
    key = (record.radar_id, record.filename)
    candidates[key] = record
    reasons.setdefault(_key(record), []).append(reason)


def plan_retention(
    catalog: Catalog,
    policy: RetentionPolicy,
    *,
    now: datetime | None = None,
) -> RetentionPlan:
    """Build a deterministic deletion plan without touching the filesystem."""
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)

    all_frames = catalog.all_frames()
    candidates: dict[tuple[str, str], FrameRecord] = {}
    reasons: dict[str, list[str]] = {}
    preserve = policy.preserve_pinned

    if policy.max_age_days is not None:
        cutoff = now - timedelta(days=policy.max_age_days)
        for record in all_frames:
            if _effective_time(record) < cutoff:
                _add_candidate(
                    candidates,
                    reasons,
                    record,
                    f"older_than_{policy.max_age_days}d",
                    preserve_pinned=preserve,
                )

    # Add oldest unpinned records until the quota is met.  Include already
    # selected age candidates but never delete more than needed for quota.
    ordered = sorted(all_frames, key=lambda item: (_effective_time(item), item.radar_id, item.filename))
    total = sum(max(0, int(record.bytes)) for record in all_frames)
    selected_bytes = sum(max(0, int(record.bytes)) for record in candidates.values())
    if policy.max_total_bytes is not None and total > policy.max_total_bytes:
        need = total - policy.max_total_bytes
        for record in ordered:
            if need <= 0:
                break
            if preserve and record.pinned:
                continue
            key = (record.radar_id, record.filename)
            if key not in candidates:
                _add_candidate(
                    candidates,
                    reasons,
                    record,
                    f"total_bytes>{policy.max_total_bytes}",
                    preserve_pinned=preserve,
                )
                selected_bytes += max(0, int(record.bytes))
            # A record selected by age still contributes to quota progress.
            need -= max(0, int(record.bytes))

    # Ensure free space threshold is satisfied based on the filesystem that
    # holds the first frame (or the database when the catalog is empty).
    if policy.min_free_bytes is not None:
        basis = Path(all_frames[0].path if all_frames else catalog.database)
        usage = shutil.disk_usage(basis if basis.is_dir() else basis.parent)
        needed = max(0, policy.min_free_bytes - usage.free)
        for record in ordered:
            if needed <= 0:
                break
            if preserve and record.pinned:
                continue
            key = (record.radar_id, record.filename)
            if key not in candidates:
                _add_candidate(
                    candidates,
                    reasons,
                    record,
                    f"free_bytes<{policy.min_free_bytes}",
                    preserve_pinned=preserve,
                )
                selected_bytes += max(0, int(record.bytes))
            needed -= max(0, int(record.bytes))

    selected = sorted(candidates.values(), key=lambda item: (_effective_time(item), item.radar_id, item.filename))
    pinned_bytes = sum(record.bytes for record in all_frames if record.pinned)
    over_quota = max(
        0,
        pinned_bytes - policy.max_total_bytes,
    ) if policy.max_total_bytes is not None else 0
    return RetentionPlan(
        catalog=catalog,
        policy=policy,
        candidates=selected,
        estimated_bytes=sum(max(0, int(record.bytes)) for record in selected),
        reasons=reasons,
        pinned_bytes_over_quota=over_quota,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def apply_retention(plan: RetentionPlan, *, dry_run: bool = True) -> RetentionResult:
    """Apply a plan conservatively, leaving mismatched records untouched."""
    result = RetentionResult(
        dry_run=dry_run,
        planned=len(plan.candidates),
        skipped_pinned=sum(1 for frame in plan.candidates if frame.pinned),
    )
    if dry_run:
        result.reclaimed_bytes = plan.estimated_bytes
        return result

    for planned in plan.candidates:
        if plan.policy.preserve_pinned and planned.pinned:
            result.skipped_pinned += 1
            continue
        current = plan.catalog.get_frame(planned.radar_id, planned.filename)
        if current is None:
            result.failed.append({"frame": _key(planned), "error": "catalog record disappeared"})
            continue
        if (
            current.path != planned.path
            or current.stored_sha256 != planned.stored_sha256
            or int(current.bytes) != int(planned.bytes)
        ):
            result.failed.append({"frame": _key(planned), "error": "catalog identity changed"})
            continue
        path = Path(current.path)
        if not path.exists() or not path.is_file():
            result.failed.append({"frame": _key(planned), "error": "archive file missing"})
            continue
        try:
            if _sha256(path) != current.stored_sha256:
                result.failed.append({"frame": _key(planned), "error": "archive hash changed"})
                continue
            path.unlink()
            preview = Path(current.preview_path) if current.preview_path else None
            if preview and preview != path and preview.exists():
                preview.unlink()
            plan.catalog.delete_frame_record(current.radar_id, current.filename)
            result.deleted.append(_key(current))
            result.reclaimed_bytes += int(current.bytes)
        except OSError as exc:
            result.failed.append({"frame": _key(planned), "error": str(exc)})
    return result


__all__ = ["RetentionPolicy", "RetentionPlan", "RetentionResult", "plan_retention", "apply_retention"]
