from __future__ import annotations

import math
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent

CACHE_DIR = Path(os.getenv("CACHE_DIR", ROOT / "cache"))
VIDEOS_DIR = Path(os.getenv("VIDEOS_DIR", ROOT / "videos"))
DATA_DIR = ROOT / "data"

POLL_INTERVAL_SEC = float(os.getenv("POLL_INTERVAL_SEC", "75"))
IMAGE_WIDTH = int(os.getenv("IMAGE_WIDTH", "2048"))
IMAGE_HEIGHT = int(os.getenv("IMAGE_HEIGHT", "2048"))
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))

USER_AGENT = "RadarVault/0.1 (local NWS radar archiver)"
WFS_URL = (
    "https://opengeo.ncep.noaa.gov/geoserver/nws/ows"
    "?service=WFS&version=1.0.0&request=GetFeature"
    "&typeName=nws:radar_sites&outputFormat=application/json"
)
WMS_OWS = "https://opengeo.ncep.noaa.gov/geoserver/ows"
PRODUCT = "sr_bref"

# Approximate radar range used for GetMap bbox (~230 km WSR-88D coverage).
RADAR_RANGE_M = 230_000

# Known sites with *_sr_bref layers (lowercase). Refreshed at runtime when possible.
SR_BREF_SUPPORT: set[str] | None = None


def ensure_dirs() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def lonlat_to_webmercator(lon: float, lat: float) -> tuple[float, float]:
    """Convert WGS84 lon/lat to EPSG:3857 meters."""
    x = lon * 20037508.342789244 / 180.0
    y = math.log(math.tan((90.0 + lat) * math.pi / 360.0)) / (math.pi / 180.0)
    y = y * 20037508.342789244 / 180.0
    return x, y


def webmercator_to_lonlat(x: float, y: float) -> tuple[float, float]:
    """Convert EPSG:3857 meters to WGS84 lon/lat."""
    lon = (x / 20037508.342789244) * 180.0
    lat = (y / 20037508.342789244) * 180.0
    lat = 180.0 / math.pi * (2.0 * math.atan(math.exp(lat * math.pi / 180.0)) - math.pi / 2.0)
    return lon, lat


def radar_bbox_3857(lon: float, lat: float, range_m: float = RADAR_RANGE_M) -> list[float]:
    cx, cy = lonlat_to_webmercator(lon, lat)
    return [cx - range_m, cy - range_m, cx + range_m, cy + range_m]


def bbox_3857_to_wgs84(bbox: list[float]) -> list[list[float]]:
    """Return Leaflet-style [[south, west], [north, east]] bounds."""
    minx, miny, maxx, maxy = bbox
    west, south = webmercator_to_lonlat(minx, miny)
    east, north = webmercator_to_lonlat(maxx, maxy)
    return [[south, west], [north, east]]
