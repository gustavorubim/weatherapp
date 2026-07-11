from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import IMAGE_HEIGHT, IMAGE_WIDTH, POLL_INTERVAL_SEC, USER_AGENT, ensure_dirs
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
        return {
            "radar_id": rid,
            "saved": saved,
            "path": str(path) if path else None,
            "sha256": digest,
            "product": product,
            "metadata": meta,
        }

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
