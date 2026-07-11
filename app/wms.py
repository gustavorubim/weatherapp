from __future__ import annotations

import hashlib
import io
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx
from PIL import Image

from app.config import IMAGE_HEIGHT, IMAGE_WIDTH, USER_AGENT, WMS_OWS, radar_bbox_3857
from app.products import preferred_product, range_for_product
from app.radars import get_radar

logger = logging.getLogger(__name__)


class WmsError(RuntimeError):
    """Raised when a WMS response is not a usable PNG."""


def resolve_product(radar_id: str, product: str | None = None) -> str:
    if product:
        return product
    resolved = preferred_product(radar_id)
    if not resolved:
        raise WmsError(
            f"Radar {radar_id.upper()} has no supported reflectivity WMS product "
            f"(expected sr_bref for WSR-88D or bref1/brefl for TDWR)"
        )
    return resolved


def layer_name(radar_id: str, product: str | None = None) -> str:
    icao = radar_id.strip().lower()
    prod = resolve_product(radar_id, product)
    return f"{icao}:{icao}_{prod}"


def workspace_wms_url(radar_id: str) -> str:
    return f"https://opengeo.ncep.noaa.gov/geoserver/{radar_id.strip().lower()}/wms"


def build_getmap_url(
    radar_id: str,
    *,
    lon: float,
    lat: float,
    width: int,
    height: int,
    bbox: list[float] | None = None,
    product: str | None = None,
) -> str:
    prod = resolve_product(radar_id, product)
    box = bbox or radar_bbox_3857(lon, lat, range_for_product(prod))
    params = {
        "service": "WMS",
        "version": "1.3.0",
        "request": "GetMap",
        "layers": layer_name(radar_id, prod),
        "styles": "",
        "crs": "EPSG:3857",
        "bbox": ",".join(str(v) for v in box),
        "width": str(width),
        "height": str(height),
        "format": "image/png",
        "transparent": "true",
    }
    return str(httpx.URL(WMS_OWS, params=params))


def _validate_png_bytes(data: bytes, expected_size: tuple[int, int] | None = None) -> Image.Image:
    if not data:
        raise WmsError("Empty WMS response")
    head = data[:200].lstrip().lower()
    if head.startswith(b"<?xml") or head.startswith(b"<html") or b"serviceexception" in head:
        snippet = data[:400].decode("utf-8", errors="replace")
        raise WmsError(f"WMS returned an error document instead of PNG: {snippet}")
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise WmsError(f"WMS response is not a PNG (magic={data[:8]!r})")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:  # noqa: BLE001
        raise WmsError(f"Invalid PNG from WMS: {exc}") from exc
    if expected_size and img.size != expected_size:
        raise WmsError(f"Unexpected image size {img.size}, expected {expected_size}")
    return img


def fetch_png_bytes(
    radar_id: str,
    *,
    width: int = IMAGE_WIDTH,
    height: int = IMAGE_HEIGHT,
    bbox: list[float] | None = None,
    product: str | None = None,
    client: httpx.Client | None = None,
) -> tuple[bytes, list[float], str]:
    """Download latest GetMap PNG bytes. Returns (png_bytes, bbox_used, product)."""
    site = get_radar(radar_id)
    if site is None:
        raise WmsError(f"Unknown radar id: {radar_id}")

    prod = resolve_product(radar_id, product)
    box = bbox or radar_bbox_3857(site["lon"], site["lat"], range_for_product(prod))
    url = build_getmap_url(
        radar_id,
        lon=site["lon"],
        lat=site["lat"],
        width=width,
        height=height,
        bbox=box,
        product=prod,
    )

    owns_client = client is None
    client = client or httpx.Client(timeout=90.0, headers={"User-Agent": USER_AGENT}, trust_env=False)
    try:
        resp = client.get(url)
        if resp.status_code >= 400:
            raise WmsError(f"WMS HTTP {resp.status_code}: {resp.text[:300]}")
        _validate_png_bytes(resp.content, expected_size=(width, height))
        return resp.content, box, prod
    finally:
        if owns_client:
            client.close()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_latest_frame(
    radar_id: str,
    *,
    width: int = IMAGE_WIDTH,
    height: int = IMAGE_HEIGHT,
    out_dir: Path | None = None,
    product: str | None = None,
) -> Path:
    """Fetch latest frame and save as a timestamped PNG. Returns path."""
    radar_id = radar_id.strip().upper()
    data, _bbox, _prod = fetch_png_bytes(radar_id, width=width, height=height, product=product)
    _validate_png_bytes(data, expected_size=(width, height))

    if out_dir is None:
        from app.config import CACHE_DIR

        out_dir = CACHE_DIR / radar_id / "frames"
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    path = out_dir / f"{stamp}.png"
    path.write_bytes(data)
    return path
