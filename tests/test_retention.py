from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.catalog import Catalog, FrameRecord
from app.disk_guard import DiskGuard
from app.retention import RetentionPolicy, apply_retention, plan_retention


def add_file(catalog: Catalog, root: Path, name: str, *, age: int, size: int, pinned: bool = False) -> Path:
    path = root / name
    path.write_bytes(bytes([len(name)]) * size)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    observed = f"2026-07-{max(1, 11 - age):02d}T12:00:00Z"
    catalog.record_frame(
        FrameRecord(
            radar_id="KTBW",
            filename=name,
            path=str(path),
            preview_path=None,
            product="sr_bref",
            observed_at=observed,
            fetched_at="2026-07-11T12:00:00Z",
            width=1,
            height=1,
            media_type="application/octet-stream",
            source_sha256=digest,
            stored_sha256=digest,
            bytes=size,
            pinned=pinned,
        )
    )
    return path


def test_retention_quota_dry_run_and_apply_preserves_pinned(tmp_path: Path) -> None:
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        old = add_file(catalog, tmp_path, "old.bin", age=10, size=5)
        pinned = add_file(catalog, tmp_path, "pinned.bin", age=10, size=7, pinned=True)
        newest = add_file(catalog, tmp_path, "new.bin", age=1, size=3)
        policy = RetentionPolicy(max_total_bytes=10, max_age_days=5)
        plan = plan_retention(catalog, policy, now=datetime(2026, 7, 11, tzinfo=timezone.utc))
        assert old.name in {candidate.filename for candidate in plan.candidates}
        assert pinned.name not in {candidate.filename for candidate in plan.candidates}
        dry = apply_retention(plan, dry_run=True)
        assert dry.reclaimed_bytes == plan.estimated_bytes
        assert old.exists() and newest.exists() and pinned.exists()
        applied = apply_retention(plan, dry_run=False)
        assert applied.ok
        assert not old.exists()
        assert pinned.exists() and newest.exists()
        assert catalog.get_frame("KTBW", old.name) is None


def test_retention_identity_mismatch_is_not_deleted(tmp_path: Path) -> None:
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        path = add_file(catalog, tmp_path, "frame.bin", age=10, size=4)
        plan = plan_retention(
            catalog,
            RetentionPolicy(max_age_days=1),
            now=datetime(2026, 7, 11, tzinfo=timezone.utc),
        )
        path.write_bytes(b"changed")
        result = apply_retention(plan, dry_run=False)
        assert not result.ok
        assert path.exists()
        assert catalog.get_frame("KTBW", path.name) is not None


def test_retention_min_free_bytes_and_pinned_over_quota(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        old = add_file(catalog, tmp_path, "old.bin", age=2, size=5)
        pinned = add_file(catalog, tmp_path, "pinned.bin", age=2, size=8, pinned=True)
        monkeypatch.setattr(
            "app.retention.shutil.disk_usage",
            lambda _path: SimpleNamespace(total=100, used=95, free=5),
        )
        plan = plan_retention(
            catalog,
            RetentionPolicy(min_free_bytes=10, max_total_bytes=1),
            now=datetime(2026, 7, 11, tzinfo=timezone.utc),
        )
        assert old.name in {candidate.filename for candidate in plan.candidates}
        assert pinned.name not in {candidate.filename for candidate in plan.candidates}
        assert plan.pinned_bytes_over_quota == 7


def test_disk_guard_threshold_states(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    usage = SimpleNamespace(total=1000, used=700, free=300)
    monkeypatch.setattr("app.disk_guard.shutil.disk_usage", lambda _path: usage)
    assert DiskGuard(tmp_path, warning_free_bytes=400, critical_free_bytes=200).check().state == "warning"
    usage.free = 100
    assert DiskGuard(tmp_path, warning_free_bytes=400, critical_free_bytes=200).check().state == "critical"
    usage.free = 500
    guard = DiskGuard(tmp_path, warning_free_bytes=400, critical_free_bytes=200)
    assert guard.check().state == "ok"
    assert guard.allow_write(250)
    assert not guard.allow_write(350)
