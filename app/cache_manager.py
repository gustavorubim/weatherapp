from __future__ import annotations

import asyncio
import importlib
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.config import (
    IMAGE_HEIGHT,
    IMAGE_WIDTH,
    POLL_INTERVAL_SEC,
    RETENTION_MAX_AGE_DAYS,
    RETENTION_MAX_TOTAL_BYTES,
    RETENTION_MIN_FREE_BYTES,
    USER_AGENT,
    ensure_dirs,
)
from app.products import supports_archiving
from app.radars import get_radar
from app.storage import load_metadata, save_frame_if_new
from app.wms import WmsError, fetch_png_bytes, sha256_bytes

logger = logging.getLogger(__name__)


@dataclass
class WorkerState:
    radar_id: str
    running: bool = False
    last_error: str | None = None
    last_poll_utc: str | None = None
    last_saved: bool = False
    polls: int = 0
    saves: int = 0
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


class CacheManager:
    """Manages per-radar background polling workers."""

    def __init__(
        self,
        *,
        poll_interval_sec: float = POLL_INTERVAL_SEC,
        width: int = IMAGE_WIDTH,
        height: int = IMAGE_HEIGHT,
    ) -> None:
        self.poll_interval_sec = poll_interval_sec
        self.width = width
        self.height = height
        self._workers: dict[str, WorkerState] = {}
        self._lock = threading.RLock()
        ensure_dirs()

    def start(self, radar_id: str) -> dict[str, Any]:
        rid = radar_id.strip().upper()
        site = get_radar(rid)
        if site is None:
            raise ValueError(f"Unknown radar id: {rid}")
        if not supports_archiving(rid):
            raise ValueError(
                f"Radar {rid} has no supported reflectivity WMS layer "
                f"(WSR-88D sr_bref or TDWR bref1/brefl)"
            )

        with self._lock:
            existing = self._workers.get(rid)
            if existing and existing.running:
                return self.status_for(rid)

            state = WorkerState(radar_id=rid)
            state.running = True
            state.stop_event.clear()
            thread = threading.Thread(
                target=self._run_worker,
                args=(state,),
                name=f"cache-{rid}",
                daemon=True,
            )
            state.thread = thread
            self._workers[rid] = state
            thread.start()
            logger.info("Started cache worker for %s", rid)
            return self.status_for(rid)

    def stop(self, radar_id: str) -> dict[str, Any]:
        rid = radar_id.strip().upper()
        with self._lock:
            state = self._workers.get(rid)
            if not state:
                return {"radar_id": rid, "running": False, "message": "not running"}
            state.stop_event.set()
            state.running = False
        if state.thread and state.thread.is_alive():
            state.thread.join(timeout=5.0)
        logger.info("Stopped cache worker for %s", rid)
        return self.status_for(rid)

    def stop_all(self) -> None:
        with self._lock:
            ids = list(self._workers.keys())
        for rid in ids:
            self.stop(rid)

    def status_for(self, radar_id: str) -> dict[str, Any]:
        rid = radar_id.strip().upper()
        meta = load_metadata(rid)
        with self._lock:
            state = self._workers.get(rid)
        running = bool(state and state.running and state.thread and state.thread.is_alive())
        return {
            "radar_id": rid,
            "running": running,
            "last_frame_utc": meta.get("last_frame_utc"),
            "last_observed_at": meta.get("last_observed_at"),
            "last_fetched_at": meta.get("last_fetched_at"),
            "frame_count": meta.get("frame_count", 0),
            "disk_bytes": meta.get("disk_bytes", 0),
            "last_sha256": meta.get("last_sha256"),
            "width": meta.get("width"),
            "height": meta.get("height"),
            "poll_interval_sec": self.poll_interval_sec,
            "last_error": state.last_error if state else None,
            "last_poll_utc": state.last_poll_utc if state else None,
            "polls": state.polls if state else 0,
            "saves": state.saves if state else 0,
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            ids = set(self._workers.keys())
        # Also include radars that have on-disk metadata/frames even if not running.
        from app.config import CACHE_DIR

        if CACHE_DIR.exists():
            for p in CACHE_DIR.iterdir():
                if p.is_dir() and (p / "metadata.json").exists():
                    ids.add(p.name.upper())
        radars = {rid: self.status_for(rid) for rid in sorted(ids)}
        return {
            "radars": radars,
            "active_count": sum(1 for r in radars.values() if r["running"]),
        }

    def poll_once(self, radar_id: str, *, client: httpx.Client | None = None) -> dict[str, Any]:
        """Fetch once and save if new. Useful for CLI/tests."""
        rid = radar_id.strip().upper()
        data, bbox, product = fetch_png_bytes(
            rid, width=self.width, height=self.height, client=client
        )
        digest = sha256_bytes(data)
        path, meta, saved = save_frame_if_new(
            rid,
            data,
            digest,
            width=self.width,
            height=self.height,
            bbox_3857=bbox,
            poll_interval_sec=self.poll_interval_sec,
            product=product,
        )
        if saved:
            self._record_catalog_frame(rid, path, meta, digest, product)
            self._run_retention_guard(rid)
        return {
            "radar_id": rid,
            "saved": saved,
            "path": str(path) if path else None,
            "sha256": digest,
            "product": product,
            "metadata": meta,
        }

    def _record_catalog_frame(
        self,
        radar_id: str,
        path: Path | None,
        metadata: dict[str, Any],
        stored_sha256: str,
        product: str,
    ) -> None:
        """Record a successful save when WT4's catalog is available."""
        if path is None:
            return
        try:
            catalog_module = importlib.import_module("app.catalog")
        except ModuleNotFoundError as exc:
            if exc.name == "app.catalog":
                return
            raise
        catalog_type = getattr(catalog_module, "Catalog", None)
        record_type = getattr(catalog_module, "FrameRecord", None)
        if catalog_type is None or record_type is None:
            return
        from app.config import CATALOG_PATH

        catalog = None
        for args, kwargs in [
            ((CATALOG_PATH,), {}),
            ((), {"path": CATALOG_PATH}),
            ((), {"db_path": CATALOG_PATH}),
            ((), {}),
        ]:
            try:
                catalog = catalog_type(*args, **kwargs)
                break
            except TypeError:
                continue
        if catalog is None:
            return
        fetched_at = metadata.get("last_fetched_at")
        record = record_type(
            radar_id=radar_id,
            filename=path.name,
            path=str(path),
            preview_path=None,
            product=product,
            observed_at=metadata.get("last_observed_at"),
            fetched_at=fetched_at,
            width=int(metadata.get("width") or self.width),
            height=int(metadata.get("height") or self.height),
            media_type="image/png",
            source_sha256=stored_sha256,
            stored_sha256=stored_sha256,
            bytes=path.stat().st_size,
            pinned=False,
        )
        catalog.record_frame(record)

    def _run_retention_guard(self, radar_id: str) -> None:
        """Invoke WT4 retention only when an explicit quota is configured.

        The integration branch deliberately does not delete files itself. If
        WT4 is present, its planner owns pinned-frame semantics and atomic
        deletion; before that merge this is a safe no-op.
        """
        if not any((RETENTION_MAX_TOTAL_BYTES, RETENTION_MAX_AGE_DAYS, RETENTION_MIN_FREE_BYTES)):
            return
        try:
            retention = importlib.import_module("app.retention")
        except ModuleNotFoundError as exc:
            if exc.name == "app.retention":
                logger.warning("Retention quota configured but app.retention is not installed")
                return
            raise
        catalog_module = importlib.import_module("app.catalog")
        catalog_type = getattr(catalog_module, "Catalog", None)
        if catalog_type is None:
            return
        from app.config import CATALOG_PATH

        catalog = None
        for args, kwargs in [
            ((CATALOG_PATH,), {}),
            ((), {"path": CATALOG_PATH}),
            ((), {"db_path": CATALOG_PATH}),
            ((), {}),
        ]:
            try:
                catalog = catalog_type(*args, **kwargs)
                break
            except TypeError:
                continue
        if catalog is None:
            return
        policy = retention.RetentionPolicy(
            max_total_bytes=RETENTION_MAX_TOTAL_BYTES,
            max_age_days=RETENTION_MAX_AGE_DAYS,
            min_free_bytes=RETENTION_MIN_FREE_BYTES,
            preserve_pinned=True,
        )
        plan = retention.plan_retention(catalog, policy)
        # The collector may enforce a configured plan, but defaults remain
        # dry-run safe. WT4's apply function is responsible for not touching
        # pinned frames and for verifying deletions.
        retention.apply_retention(plan, dry_run=False)

    def _run_worker(self, state: WorkerState) -> None:
        backoff = self.poll_interval_sec
        with httpx.Client(timeout=90.0, headers={"User-Agent": USER_AGENT}, trust_env=False) as client:
            while not state.stop_event.is_set():
                try:
                    result = self.poll_once(state.radar_id, client=client)
                    state.polls += 1
                    state.last_error = None
                    state.last_saved = result["saved"]
                    if result["saved"]:
                        state.saves += 1
                    from datetime import datetime, timezone

                    state.last_poll_utc = (
                        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    )
                    backoff = self.poll_interval_sec
                except WmsError as exc:
                    state.last_error = str(exc)
                    logger.warning("%s WMS error: %s", state.radar_id, exc)
                    backoff = min(backoff * 1.5, 300)
                except Exception as exc:  # noqa: BLE001
                    state.last_error = str(exc)
                    logger.exception("%s worker error", state.radar_id)
                    backoff = min(backoff * 1.5, 300)

                # Interruptible sleep
                state.stop_event.wait(backoff)

        state.running = False


# Process-wide manager used by the API.
manager = CacheManager()


async def run_for_duration(radar_id: str, duration_sec: float) -> dict[str, Any]:
    """Start a worker, wait, then stop — for CLI smoke tests."""
    manager.start(radar_id)
    try:
        await asyncio.sleep(duration_sec)
    finally:
        manager.stop(radar_id)
    return manager.status_for(radar_id)
