from __future__ import annotations

import importlib
import inspect
import hashlib
import logging
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import __version__
from app import config
from app.cache_manager import manager
from app.config import ROOT, VIDEOS_DIR, bbox_3857_to_wgs84, ensure_dirs, radar_bbox_3857
from app.products import preferred_product, range_for_product, supports_archiving
from app.radars import fetch_radar_sites, get_radar
from app.storage import frames_dir, list_frames, load_metadata, parse_iso_utc
from app.video import VideoError, ensure_ffmpeg, export_video

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("radarvault")

ensure_dirs()
STATIC_DIR = ROOT / "static"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def _jsonable(value: Any) -> Any:
    if hasattr(value, "as_dict") and callable(value.as_dict):
        return _jsonable(value.as_dict())
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "tolist") and callable(value.tolist):
        return value.tolist()
    return value


def _frame_response(radar_id: str, frame: dict[str, Any]) -> dict[str, Any]:
    rid = radar_id.strip().upper()
    filename = str(frame["filename"])
    frame_url = f"/api/cache/{quote(rid, safe='')}/frame/{quote(filename, safe='')}"
    preview_path = frame.get("preview_path")
    preview_name = Path(preview_path).name if preview_path else None
    preview_url = (
        f"/api/cache/{quote(rid, safe='')}/preview/{quote(preview_name, safe='')}"
        if preview_name
        else frame_url
    )
    result = {
        "filename": filename,
        "utc": frame.get("utc"),
        "observed_at": frame.get("observed_at"),
        "fetched_at": frame.get("fetched_at"),
        "preview_url": preview_url,
        "url": frame_url,
        "size": frame.get("size", 0),
        "width": frame.get("width"),
        "height": frame.get("height"),
        "media_type": frame.get("media_type", "image/png"),
        "source_sha256": frame.get("source_sha256"),
        "stored_sha256": frame.get("stored_sha256"),
    }
    return result


def _parse_optional_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parse_iso_utc(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid UTC timestamp: {value}") from exc


def _optional_module(name: str) -> Any | None:
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name == name:
            return None
        raise


def _catalog() -> Any | None:
    """Load WT4's catalog lazily so WT7 remains usable before that merge."""
    module = _optional_module("app.catalog")
    if module is None or not hasattr(module, "Catalog"):
        return None
    catalog_type = module.Catalog
    for args, kwargs in [
        ((config.CATALOG_PATH,), {}),
        ((), {"path": config.CATALOG_PATH}),
        ((), {"db_path": config.CATALOG_PATH}),
        ((), {}),
    ]:
        try:
            return catalog_type(*args, **kwargs)
        except TypeError:
            continue
    return None


@dataclass
class _VideoJob:
    job_id: str
    radar_id: str
    start: str | None
    end: str | None
    fps: float
    quality: str
    dimension_policy: str
    timestamp_overlay: bool
    state: str = "queued"
    progress: float = 0.0
    message: str = "Queued"
    path: str | None = None
    error: str | None = None
    created_at: str = field(default_factory=lambda: _iso(_utc_now()) or "")
    updated_at: str = field(default_factory=lambda: _iso(_utc_now()) or "")
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def response(self) -> dict[str, Any]:
        filename = Path(self.path).name if self.path else None
        return {
            "job_id": self.job_id,
            "radar_id": self.radar_id,
            "state": self.state,
            "progress": round(self.progress, 4),
            "message": self.message,
            "filename": filename,
            "path": self.path,
            "download_url": f"/videos/{quote(filename, safe='')}" if filename else None,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class _VideoJobManager:
    """Small WT7 adapter for WT5's VideoJobManager contract.

    Once WT5 is merged, this adapter can delegate to its implementation. The
    fallback keeps the API asynchronous on the integration branch today and
    supports cancellation/progress when the exporter accepts those keywords.
    """

    def __init__(self, max_workers: int) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="video-job")
        self._jobs: dict[str, _VideoJob] = {}
        self._lock = threading.RLock()

    def submit(self, request: "VideoJobRequest") -> dict[str, Any]:
        job = _VideoJob(
            job_id=uuid.uuid4().hex,
            radar_id=request.radar_id.strip().upper(),
            start=request.start,
            end=request.end,
            fps=request.fps,
            quality=request.quality,
            dimension_policy=request.dimension_policy,
            timestamp_overlay=request.timestamp_overlay,
        )
        with self._lock:
            self._jobs[job.job_id] = job
        self._executor.submit(self._run, job)
        return self.status(job.job_id) or job.response()

    def status(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.response() if job else None

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            job.cancel_event.set()
            job.updated_at = _iso(_utc_now()) or job.updated_at
            if job.state == "queued":
                job.state = "cancelled"
                job.message = "Cancelled before export started"
            elif job.state == "running":
                job.message = "Cancellation requested"
            return job.response()

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _update(self, job: _VideoJob, **changes: Any) -> None:
        with self._lock:
            if job.state == "cancelled" and changes.get("state") not in {None, "cancelled"}:
                return
            for key, value in changes.items():
                setattr(job, key, value)
            job.updated_at = _iso(_utc_now()) or job.updated_at

    def _run(self, job: _VideoJob) -> None:
        if job.cancel_event.is_set():
            self._update(job, state="cancelled", message="Cancelled before export started")
            return
        self._update(job, state="running", progress=0.0, message="Starting export")

        def progress(value: float, message: str) -> None:
            self._update(job, progress=max(0.0, min(float(value), 1.0)), message=message)

        kwargs: dict[str, Any] = {
            "start": job.start,
            "end": job.end,
            "fps": job.fps,
            "quality": job.quality,
            "dimension_policy": job.dimension_policy,
            "timestamp_overlay": job.timestamp_overlay,
            "progress_callback": progress,
            "cancel_event": job.cancel_event,
        }
        try:
            # WT5 accepts the full contract; the legacy v1 exporter accepts
            # only start/end/fps, so retain that compatibility during bring-up.
            try:
                path = export_video(job.radar_id, **kwargs)
            except TypeError as exc:
                if "unexpected keyword" not in str(exc):
                    raise
                path = export_video(
                    job.radar_id,
                    start=job.start,
                    end=job.end,
                    fps=job.fps,
                )
            if job.cancel_event.is_set():
                self._update(job, state="cancelled", message="Export cancelled")
            else:
                self._update(
                    job,
                    state="complete",
                    progress=1.0,
                    message="Export complete",
                    path=str(path),
                )
        except VideoError as exc:
            self._update(job, state="failed", message="Export failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Video job %s failed", job.job_id)
            self._update(job, state="failed", message="Export failed", error=str(exc))


video_jobs = _VideoJobManager(config.JOB_CONCURRENCY)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_dirs()
    try:
        ensure_ffmpeg()
        logger.info("ffmpeg available")
    except VideoError as exc:
        logger.warning("%s", exc)
    try:
        sites = fetch_radar_sites()
        logger.info("Radar inventory ready: %d sites", len(sites))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not preload radar sites: %s", exc)
    yield
    manager.stop_all()
    video_jobs.shutdown()


app = FastAPI(title="RadarVault", version=__version__, lifespan=lifespan)


class ExportRequest(BaseModel):
    radar_id: str
    start: Optional[str] = None
    end: Optional[str] = None
    fps: float = Field(default=15, ge=1, le=60)
    quality: str = "balanced"
    dimension_policy: str = "error"
    timestamp_overlay: bool = False


class VideoJobRequest(ExportRequest):
    pass


class RetentionPlanRequest(BaseModel):
    max_total_bytes: Optional[int] = Field(default=None, gt=0)
    max_age_days: Optional[int] = Field(default=None, gt=0)
    min_free_bytes: Optional[int] = Field(default=None, gt=0)
    preserve_pinned: bool = True


class NowcastRequest(BaseModel):
    lead_minutes: int = Field(default=30, ge=1, le=180)


@app.get("/api/health")
def health() -> dict[str, Any]:
    ffmpeg_ok = True
    try:
        ensure_ffmpeg()
    except VideoError:
        ffmpeg_ok = False
    return {
        "status": "ok",
        "version": __version__,
        "ffmpeg": ffmpeg_ok,
        "analysis_enabled": config.ANALYSIS_ENABLED,
    }


@app.get("/api/radars")
def api_radars() -> list[dict[str, Any]]:
    try:
        return fetch_radar_sites()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Failed to load radar sites: {exc}") from exc


@app.get("/api/radars/{radar_id}")
def api_radar(radar_id: str) -> dict[str, Any]:
    site = get_radar(radar_id)
    if not site:
        raise HTTPException(status_code=404, detail=f"Unknown radar: {radar_id}")
    site = dict(site)
    site["supports_archive"] = supports_archiving(radar_id)
    meta = load_metadata(radar_id)
    site["metadata"] = meta
    product = meta.get("product") or preferred_product(radar_id)
    bbox = meta.get("bbox_3857")
    if not bbox and product:
        bbox = radar_bbox_3857(site["lon"], site["lat"], range_for_product(product))
    site["bounds"] = bbox_3857_to_wgs84(bbox) if bbox else None
    return site


def _frames_for_api(
    radar_id: str,
    *,
    start: str | None,
    end: str | None,
    after: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    try:
        frames = list_frames(
            radar_id,
            start=_parse_optional_utc(start),
            end=_parse_optional_utc(end),
            after=after,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid frame range: {exc}") from exc
    return [_frame_response(radar_id, frame) for frame in frames]


@app.get("/api/cache/{radar_id}/frames")
def api_frames(
    radar_id: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    after: Optional[str] = None,
    limit: int = Query(500, ge=1, le=5000),
) -> list[dict[str, Any]]:
    return _frames_for_api(radar_id, start=start, end=end, after=after, limit=limit)


@app.get("/api/cache/{radar_id}/overlay")
def api_overlay(
    radar_id: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    after: Optional[str] = None,
    limit: int = Query(500, ge=1, le=5000),
) -> dict[str, Any]:
    """Return bounded frames plus geographic bounds for map overlay playback."""
    site = get_radar(radar_id)
    if not site:
        raise HTTPException(status_code=404, detail=f"Unknown radar: {radar_id}")
    meta = load_metadata(radar_id)
    product = meta.get("product") or preferred_product(radar_id)
    bbox = meta.get("bbox_3857")
    if not bbox:
        if not product:
            raise HTTPException(status_code=400, detail="No product/bbox available yet — archive at least one frame")
        bbox = radar_bbox_3857(site["lon"], site["lat"], range_for_product(product))
    return {
        "radar_id": radar_id.strip().upper(),
        "product": product,
        "bounds": bbox_3857_to_wgs84(bbox),
        "bbox_3857": bbox,
        "frames": _frames_for_api(radar_id, start=start, end=end, after=after, limit=limit),
    }


@app.post("/api/cache/{radar_id}/start")
def api_cache_start(radar_id: str) -> dict[str, Any]:
    try:
        return manager.start(radar_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/cache/{radar_id}/stop")
def api_cache_stop(radar_id: str) -> dict[str, Any]:
    return manager.stop(radar_id)


@app.get("/api/cache/status")
def api_cache_status() -> dict[str, Any]:
    return manager.status()


@app.get("/api/cache/{radar_id}/latest")
def api_latest_frame(radar_id: str):
    frames = list_frames(radar_id, limit=5000)
    if not frames:
        raise HTTPException(status_code=404, detail="No cached frames")
    path = Path(frames[-1]["path"])
    return FileResponse(path, media_type=frames[-1].get("media_type", "image/png"), filename=path.name)


@app.get("/api/cache/{radar_id}/frame/{filename}")
def api_frame(radar_id: str, filename: str):
    if not filename or Path(filename).name != filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = frames_dir(radar_id) / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Frame not found")
    media_type = {
        ".png": "image/png",
        ".png8": "image/png",
        ".webp": "image/webp",
    }.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media_type, filename=filename)


@app.get("/api/cache/{radar_id}/preview/{filename}")
def api_preview(radar_id: str, filename: str):
    if not filename or Path(filename).name != filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = frames_dir(radar_id).parent / "previews" / filename
    if not path.is_file() or path.suffix.lower() != ".webp":
        raise HTTPException(status_code=404, detail="Preview not found")
    return FileResponse(path, media_type="image/webp", filename=filename)


def _storage_status() -> dict[str, Any]:
    root = config.CACHE_DIR
    root.mkdir(parents=True, exist_ok=True)
    used_bytes = 0
    frame_count = 0
    for path in root.rglob("*"):
        if path.is_file():
            used_bytes += path.stat().st_size
            if path.parent.name == "frames":
                frame_count += 1
    usage = shutil.disk_usage(root)
    catalog = _catalog()
    catalog_stats = None
    if catalog is not None and hasattr(catalog, "global_stats"):
        try:
            catalog_stats = catalog.global_stats()
        except Exception:  # noqa: BLE001
            logger.exception("Could not read catalog statistics")
    return {
        "cache_dir": str(root),
        "catalog_path": str(config.CATALOG_PATH),
        "bytes": used_bytes,
        "frame_count": frame_count,
        "total_bytes": usage.total,
        "free_bytes": usage.free,
        "configured": {
            "archive_format": config.ARCHIVE_FORMAT,
            "preview_max_dimension": config.PREVIEW_MAX_DIMENSION,
            "retention_max_total_bytes": config.RETENTION_MAX_TOTAL_BYTES,
            "retention_max_age_days": config.RETENTION_MAX_AGE_DAYS,
            "retention_min_free_bytes": config.RETENTION_MIN_FREE_BYTES,
        },
        "catalog": catalog_stats,
    }


@app.get("/api/storage/status")
def api_storage_status() -> dict[str, Any]:
    return _storage_status()


def _legacy_retention_plan(request: RetentionPlanRequest) -> dict[str, Any]:
    now = _utc_now()
    max_total = request.max_total_bytes if request.max_total_bytes is not None else config.RETENTION_MAX_TOTAL_BYTES
    max_age = request.max_age_days if request.max_age_days is not None else config.RETENTION_MAX_AGE_DAYS
    min_free = request.min_free_bytes if request.min_free_bytes is not None else config.RETENTION_MIN_FREE_BYTES
    status = _storage_status()
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    frames: list[dict[str, Any]] = []
    for radar_dir in sorted(config.CACHE_DIR.iterdir()) if config.CACHE_DIR.exists() else []:
        if not radar_dir.is_dir():
            continue
        frames.extend(list_frames(radar_dir.name, limit=5000))
    frames.sort(key=lambda frame: frame["timestamp"])
    cutoff = now - timedelta(days=max_age) if max_age else None
    disk_deficit = max(0, (min_free or 0) - status["free_bytes"])
    quota_deficit = max(0, status["bytes"] - max_total) if max_total else 0
    bytes_needed = max(disk_deficit, quota_deficit)
    for frame in frames:
        detail = load_metadata(Path(frame["path"]).parents[1].name).get("frames", {})
        detail = detail.get(frame["filename"], {}) if isinstance(detail, dict) else {}
        if request.preserve_pinned and detail.get("pinned"):
            continue
        age_match = bool(cutoff and frame["timestamp"] < cutoff)
        quota_match = bytes_needed > 0
        if not age_match and not quota_match:
            continue
        key = frame["path"]
        if key in seen:
            continue
        seen.add(key)
        item = {
            "radar_id": Path(frame["path"]).parents[1].name,
            "filename": frame["filename"],
            "path": key,
            "bytes": frame["size"],
            "utc": frame["utc"],
            "pinned": False,
        }
        candidates.append(item)
        bytes_needed = max(0, bytes_needed - frame["size"])
        if cutoff is None and bytes_needed == 0:
            break
    return {
        "dry_run": True,
        "policy": {
            "max_total_bytes": max_total,
            "max_age_days": max_age,
            "min_free_bytes": min_free,
            "preserve_pinned": request.preserve_pinned,
        },
        "current_bytes": status["bytes"],
        "free_bytes": status["free_bytes"],
        "candidate_bytes": sum(item["bytes"] for item in candidates),
        "candidates": candidates,
        "candidate_count": len(candidates),
    }


@app.post("/api/storage/retention/plan")
def api_retention_plan(request: RetentionPlanRequest) -> dict[str, Any]:
    # Prefer WT4's richer catalog/retention implementation when it is present.
    retention = _optional_module("app.retention")
    catalog = _catalog()
    if retention is not None and catalog is not None and hasattr(retention, "plan_retention"):
        try:
            # A moved/legacy archive may not have been indexed yet. Keep the
            # filesystem planner authoritative until the catalog rebuild has
            # records for this archive.
            stats = catalog.global_stats() if hasattr(catalog, "global_stats") else {}
            if int(stats.get("frame_count", 0)) == 0:
                return _legacy_retention_plan(request)
            policy = retention.RetentionPolicy(
                max_total_bytes=request.max_total_bytes,
                max_age_days=request.max_age_days,
                min_free_bytes=request.min_free_bytes,
                preserve_pinned=request.preserve_pinned,
            )
            plan = retention.plan_retention(catalog, policy, now=_utc_now())
            return plan.as_dict() if hasattr(plan, "as_dict") else _jsonable(plan)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Catalog retention planner unavailable; using legacy planner: %s", exc)
    return _legacy_retention_plan(request)


def _run_export(request: ExportRequest) -> Path:
    kwargs: dict[str, Any] = {
        "start": request.start,
        "end": request.end,
        "fps": request.fps,
        "quality": request.quality,
        "dimension_policy": request.dimension_policy,
        "timestamp_overlay": request.timestamp_overlay,
    }
    try:
        return export_video(request.radar_id, **kwargs)
    except TypeError as exc:
        if "unexpected keyword" not in str(exc):
            raise
        return export_video(
            request.radar_id,
            start=request.start,
            end=request.end,
            fps=request.fps,
        )


@app.post("/api/videos/export")
def api_export(request: ExportRequest) -> dict[str, Any]:
    try:
        path = _run_export(request)
    except VideoError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rel = path.name
    return {
        "ok": True,
        "path": str(path),
        "filename": rel,
        "bytes": path.stat().st_size,
        "download_url": f"/videos/{quote(rel, safe='')}",
        "status": "complete",
    }


@app.post("/api/videos/jobs")
def api_video_job_submit(request: VideoJobRequest) -> dict[str, Any]:
    if request.quality not in {"archive", "balanced", "small"}:
        raise HTTPException(status_code=400, detail="quality must be archive, balanced, or small")
    if request.dimension_policy not in {"error", "normalize"}:
        raise HTTPException(status_code=400, detail="dimension_policy must be error or normalize")
    return video_jobs.submit(request)


@app.get("/api/videos/jobs/{job_id}")
def api_video_job_status(job_id: str) -> dict[str, Any]:
    status = video_jobs.status(job_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Video job not found")
    return status


@app.post("/api/videos/jobs/{job_id}/cancel")
def api_video_job_cancel(job_id: str) -> dict[str, Any]:
    status = video_jobs.cancel(job_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Video job not found")
    return status


@app.get("/api/analysis/{radar_id}/cells")
def api_analysis_cells(radar_id: str) -> dict[str, Any]:
    frames = list_frames(radar_id, limit=5000)
    latest = frames[-1] if frames else None
    source_hash = latest.get("source_sha256") if latest else None
    if latest and not source_hash:
        try:
            source_hash = hashlib.sha256(Path(latest["path"]).read_bytes()).hexdigest()
        except OSError:
            source_hash = None
    provenance = {
        "source_frame": latest["filename"] if latest else None,
        "source_sha256": source_hash,
        "parameters": {"analysis_enabled": config.ANALYSIS_ENABLED},
        "experimental": True,
    }
    analysis = _optional_module("app.analysis") if config.ANALYSIS_ENABLED else None
    if analysis is None or latest is None or not hasattr(analysis, "detect_cells"):
        return {"radar_id": radar_id.strip().upper(), "enabled": False, "cells": [], "provenance": provenance}
    try:
        from PIL import Image

        with Image.open(latest["path"]) as image:
            cells = analysis.detect_cells(image, min_bin=20, min_pixels=20)
        return {
            "radar_id": radar_id.strip().upper(),
            "enabled": True,
            "cells": _jsonable(cells),
            "provenance": provenance,
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Analysis failed: {exc}") from exc


@app.post("/api/analysis/{radar_id}/nowcast")
def api_analysis_nowcast(radar_id: str, request: NowcastRequest) -> dict[str, Any]:
    frames = list_frames(radar_id, limit=5000)
    provenance = {
        "source_frames": [frame["filename"] for frame in frames[-2:]],
        "source_sha256": [frame.get("stored_sha256") for frame in frames[-2:]],
        "parameters": {"lead_minutes": request.lead_minutes, "analysis_enabled": config.ANALYSIS_ENABLED},
        "experimental": True,
    }
    analysis = _optional_module("app.analysis") if config.ANALYSIS_ENABLED else None
    if analysis is None:
        return {
            "radar_id": radar_id.strip().upper(),
            "enabled": False,
            "status": "analysis_unavailable",
            "nowcast": None,
            "provenance": provenance,
        }
    if len(frames) < 2 or not hasattr(analysis, "nowcast_from_frames"):
        return {
            "radar_id": radar_id.strip().upper(),
            "enabled": True,
            "status": "insufficient_frames",
            "nowcast": None,
            "provenance": provenance,
        }
    try:
        from PIL import Image

        selected = frames[-2:]
        images = []
        for frame in selected:
            with Image.open(frame["path"]) as image:
                images.append(image.convert("RGBA"))
        timestamps = [frame.get("observed_at") or frame.get("utc") for frame in selected]
        result = analysis.nowcast_from_frames(
            images,
            timestamps,
            lead_minutes=request.lead_minutes,
            method="advection",
        )
        prediction = result.prediction
        return {
            "radar_id": radar_id.strip().upper(),
            "enabled": True,
            "status": "complete",
            "nowcast": {
                "method": result.method,
                "lead_minutes": result.lead_minutes,
                "shape": list(prediction.shape),
                "min_bin": int(prediction.min()),
                "max_bin": int(prediction.max()),
                "active_pixels": int((prediction > 0).sum()),
                "experimental": result.experimental,
            },
            "provenance": _jsonable(result.provenance),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Nowcast failed: {exc}") from exc


@app.get("/videos/{filename}")
def download_video(filename: str):
    if not filename or Path(filename).name != filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = VIDEOS_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Video not found")
    return FileResponse(path, media_type="video/mp4", filename=filename)


@app.get("/")
def index():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=500, detail="UI missing")
    return FileResponse(index_path)


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
