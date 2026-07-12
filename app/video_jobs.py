"""Process-local background video export jobs."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.video import VideoError, export_video

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class VideoJobRequest:
    radar_id: str
    start: str = "2020-01-01"
    end: str = "2099-01-01"
    fps: float = 15
    out: str | None = None
    quality: str = "balanced"
    dimension_policy: str = "error"
    target_width: int | None = None
    target_height: int | None = None
    timestamp_overlay: bool = False
    reuse_completed: bool = True

    def fingerprint(self) -> str:
        payload = (
            f"{self.radar_id}|{self.start}|{self.end}|{self.fps}|"
            f"{self.quality}|{self.dimension_policy}|{self.target_width}|"
            f"{self.target_height}|{self.timestamp_overlay}|{self.out or ''}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class VideoJobStatus:
    job_id: str
    state: str
    progress: float = 0.0
    message: str = ""
    radar_id: str = ""
    request_fingerprint: str = ""
    output_path: str | None = None
    error: str | None = None
    log_tail: str | None = None
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class VideoJobManager:
    """In-process video job queue with progress, cancel, reuse, and cleanup."""

    def __init__(
        self,
        *,
        max_concurrent: int = 1,
        retention_seconds: float = 3600,
        export_fn: Callable[..., Path] | None = None,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self.max_concurrent = max_concurrent
        self.retention_seconds = retention_seconds
        self._export_fn = export_fn or export_video
        self._lock = threading.RLock()
        self._jobs: dict[str, VideoJobStatus] = {}
        self._requests: dict[str, VideoJobRequest] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._fingerprint_index: dict[str, str] = {}
        self._running = 0
        self._queue: list[str] = []

    def submit(self, request: VideoJobRequest) -> VideoJobStatus:
        self.cleanup()
        fp = request.fingerprint()
        with self._lock:
            if request.reuse_completed:
                existing_id = self._fingerprint_index.get(fp)
                if existing_id:
                    existing = self._jobs.get(existing_id)
                    if existing and existing.state == "complete" and existing.output_path:
                        out = Path(existing.output_path)
                        if out.exists() and out.stat().st_size > 0:
                            return VideoJobStatus(**asdict(existing))

            job_id = uuid.uuid4().hex
            status = VideoJobStatus(
                job_id=job_id,
                state="queued",
                progress=0.0,
                message="queued",
                radar_id=request.radar_id.strip().upper(),
                request_fingerprint=fp,
            )
            self._jobs[job_id] = status
            self._requests[job_id] = request
            self._cancel[job_id] = threading.Event()
            self._fingerprint_index[fp] = job_id
            self._queue.append(job_id)

        self._pump()
        return self.status(job_id)

    def status(self, job_id: str) -> VideoJobStatus:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(f"Unknown job_id: {job_id}")
            return VideoJobStatus(**asdict(job))

    def cancel(self, job_id: str) -> VideoJobStatus:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(f"Unknown job_id: {job_id}")
            if job.state in {"complete", "failed", "cancelled"}:
                return VideoJobStatus(**asdict(job))
            self._cancel[job_id].set()
            if job.state == "queued" and job_id in self._queue:
                self._queue.remove(job_id)
                job.state = "cancelled"
                job.message = "cancelled"
                job.progress = 0.0
                job.updated_at = _utc_now()
        return self.status(job_id)

    def cleanup(self) -> int:
        cutoff = time.time() - self.retention_seconds
        removed = 0
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                if job.state not in {"complete", "failed", "cancelled"}:
                    continue
                try:
                    ts = datetime.fromisoformat(job.updated_at.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
                if ts >= cutoff:
                    continue
                self._jobs.pop(job_id, None)
                self._requests.pop(job_id, None)
                self._cancel.pop(job_id, None)
                self._threads.pop(job_id, None)
                if self._fingerprint_index.get(job.request_fingerprint) == job_id:
                    self._fingerprint_index.pop(job.request_fingerprint, None)
                removed += 1
        return removed

    def _pump(self) -> None:
        with self._lock:
            while self._running < self.max_concurrent and self._queue:
                job_id = self._queue.pop(0)
                job = self._jobs[job_id]
                if job.state != "queued":
                    continue
                req = self._requests.get(job_id)
                if req is None:
                    job.state = "failed"
                    job.error = "Internal error: missing request"
                    job.updated_at = _utc_now()
                    continue
                job.state = "running"
                job.message = "starting"
                job.updated_at = _utc_now()
                self._running += 1
                t = threading.Thread(
                    target=self._run_job,
                    args=(job_id, req),
                    name=f"video-job-{job_id[:8]}",
                    daemon=True,
                )
                self._threads[job_id] = t
                t.start()

    def _run_job(self, job_id: str, request: VideoJobRequest) -> None:
        cancel_event = self._cancel[job_id]

        def on_progress(p: float, msg: str) -> None:
            with self._lock:
                job = self._jobs[job_id]
                if job.state == "cancelled":
                    return
                job.progress = max(job.progress, float(p))
                job.message = msg
                job.updated_at = _utc_now()

        try:
            path = self._export_fn(
                request.radar_id,
                start=request.start,
                end=request.end,
                fps=request.fps,
                out=Path(request.out) if request.out else None,
                quality=request.quality,
                dimension_policy=request.dimension_policy,
                target_width=request.target_width,
                target_height=request.target_height,
                timestamp_overlay=request.timestamp_overlay,
                progress_callback=on_progress,
                cancel_event=cancel_event,
            )
            with self._lock:
                job = self._jobs[job_id]
                if cancel_event.is_set():
                    job.state = "cancelled"
                    job.message = "cancelled"
                    job.error = "Export cancelled"
                    partial = Path(str(path) + ".partial") if path else None
                    if partial and partial.exists():
                        partial.unlink(missing_ok=True)
                else:
                    job.state = "complete"
                    job.progress = 1.0
                    job.message = "complete"
                    job.output_path = str(path)
                    job.error = None
                job.updated_at = _utc_now()
        except VideoError as exc:
            msg = str(exc)
            with self._lock:
                job = self._jobs[job_id]
                if "cancel" in msg.lower() or cancel_event.is_set():
                    job.state = "cancelled"
                    job.message = "cancelled"
                    job.error = msg
                else:
                    job.state = "failed"
                    job.message = "failed"
                    job.error = msg.split("\n", 1)[0][:300]
                    job.log_tail = msg[-2000:]
                job.updated_at = _utc_now()
        except Exception as exc:  # noqa: BLE001
            logger.exception("video job %s crashed", job_id)
            with self._lock:
                job = self._jobs[job_id]
                job.state = "failed"
                job.message = "failed"
                job.error = str(exc)[:300]
                job.log_tail = str(exc)[-2000:]
                job.updated_at = _utc_now()
        finally:
            with self._lock:
                self._running = max(0, self._running - 1)
            self._pump()


default_manager = VideoJobManager()
