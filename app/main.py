from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import __version__
from app.cache_manager import manager
from app.config import ROOT, VIDEOS_DIR, bbox_3857_to_wgs84, ensure_dirs
from app.products import preferred_product, range_for_product, supports_archiving
from app.radars import fetch_radar_sites, get_radar
from app.storage import frames_dir, list_frames, load_metadata, parse_iso_utc
from app.video import VideoError, ensure_ffmpeg, export_video
from app.config import radar_bbox_3857

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("radarvault")

ensure_dirs()

STATIC_DIR = ROOT / "static"


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


app = FastAPI(title="RadarVault", version=__version__, lifespan=lifespan)


class ExportRequest(BaseModel):
    radar_id: str
    start: str = "2020-01-01T00:00:00Z"
    end: str = "2099-01-01T00:00:00Z"
    fps: float = Field(default=15, ge=1, le=60)


@app.get("/api/health")
def health() -> dict[str, Any]:
    ffmpeg_ok = True
    try:
        ensure_ffmpeg()
    except VideoError:
        ffmpeg_ok = False
    return {"status": "ok", "version": __version__, "ffmpeg": ffmpeg_ok}


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


@app.get("/api/cache/{radar_id}/overlay")
def api_overlay(radar_id: str, start: Optional[str] = None, end: Optional[str] = None) -> dict[str, Any]:
    """Frames + geographic bounds for map overlay playback."""
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
    frames = list_frames(
        radar_id,
        start=parse_iso_utc(start) if start else None,
        end=parse_iso_utc(end) if end else None,
    )
    return {
        "radar_id": radar_id.strip().upper(),
        "product": product,
        "bounds": bbox_3857_to_wgs84(bbox),
        "bbox_3857": bbox,
        "frames": [
            {
                "filename": f["filename"],
                "utc": f["utc"],
                "url": f"/api/cache/{radar_id.strip().upper()}/frame/{f['filename']}",
            }
            for f in frames
        ],
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


@app.get("/api/cache/{radar_id}/frames")
def api_frames(
    radar_id: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> list[dict[str, Any]]:
    frames = list_frames(
        radar_id,
        start=parse_iso_utc(start) if start else None,
        end=parse_iso_utc(end) if end else None,
    )
    return [
        {"filename": f["filename"], "utc": f["utc"], "size": f["size"]}
        for f in frames
    ]


@app.get("/api/cache/{radar_id}/latest")
def api_latest_frame(radar_id: str):
    frames = list_frames(radar_id)
    if not frames:
        raise HTTPException(status_code=404, detail="No cached frames")
    path = Path(frames[-1]["path"])
    return FileResponse(path, media_type="image/png", filename=path.name)


@app.get("/api/cache/{radar_id}/frame/{filename}")
def api_frame(radar_id: str, filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = frames_dir(radar_id) / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Frame not found")
    return FileResponse(path, media_type="image/png", filename=filename)


@app.post("/api/videos/export")
def api_export(req: ExportRequest) -> dict[str, Any]:
    try:
        path = export_video(req.radar_id, start=req.start, end=req.end, fps=req.fps)
    except VideoError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rel = path.name
    return {
        "ok": True,
        "path": str(path),
        "filename": rel,
        "bytes": path.stat().st_size,
        "download_url": f"/videos/{rel}",
        "status": "complete",
    }


@app.get("/videos/{filename}")
def download_video(filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = VIDEOS_DIR / filename
    if not path.exists():
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
