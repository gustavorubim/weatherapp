from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.config import DATA_DIR, USER_AGENT, WFS_URL, ensure_dirs
from app.products import discover_product_index, preferred_product, product_info, supports_archiving

logger = logging.getLogger(__name__)

_RADARS_CACHE: list[dict[str, Any]] | None = None


def _normalize_site(props: dict[str, Any]) -> dict[str, Any] | None:
    radar_id = (props.get("rda_id") or "").strip().upper()
    if not radar_id:
        return None
    try:
        lat = float(props["lat"])
        lon = float(props["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "id": radar_id,
        "name": props.get("name") or radar_id,
        "lat": lat,
        "lon": lon,
        "elevmeter": props.get("elevmeter"),
        "wfo_id": props.get("wfo_id"),
    }


def _annotate_site(site: dict[str, Any]) -> dict[str, Any]:
    info = product_info(site["id"])
    product = info["preferred"] if info else None
    site["product"] = product
    site["products"] = info["products"] if info else []
    site["kind"] = info["kind"] if info else None
    site["supports_archive"] = product is not None
    # Back-compat for older UI field
    site["supports_sr_bref"] = product == "sr_bref"
    return site


def fetch_radar_sites(*, use_cache: bool = True, force_refresh: bool = False) -> list[dict[str, Any]]:
    """Load NWS radar sites from WFS (optionally cached to data/radars.json)."""
    global _RADARS_CACHE
    ensure_dirs()
    cache_path = DATA_DIR / "radars.json"

    # Ensure product index is warm so annotations are accurate.
    discover_product_index(force_refresh=force_refresh)

    if use_cache and not force_refresh:
        if _RADARS_CACHE is not None:
            return list(_RADARS_CACHE)
        if cache_path.exists():
            try:
                sites = json.loads(cache_path.read_text())
                if isinstance(sites, list) and len(sites) >= 150:
                    # Re-annotate in case product index improved.
                    sites = [_annotate_site(dict(s)) for s in sites]
                    # Upgrade stale caches that lack supports_archive / product.
                    if any("supports_archive" not in s for s in sites):
                        pass
                    else:
                        _RADARS_CACHE = sites
                        return list(sites)
                    _RADARS_CACHE = sites
                    return list(sites)
            except (json.JSONDecodeError, OSError):
                pass

    with httpx.Client(timeout=60.0, headers={"User-Agent": USER_AGENT}, trust_env=False) as client:
        resp = client.get(WFS_URL)
        resp.raise_for_status()
        payload = resp.json()

    sites: list[dict[str, Any]] = []
    for feature in payload.get("features", []):
        site = _normalize_site(feature.get("properties") or {})
        if site:
            sites.append(_annotate_site(site))

    sites.sort(key=lambda s: s["id"])
    cache_path.write_text(json.dumps(sites, indent=2))
    _RADARS_CACHE = sites
    logger.info("Loaded %d radar sites from WFS", len(sites))
    return list(sites)


def get_radar(radar_id: str) -> dict[str, Any] | None:
    rid = radar_id.strip().upper()
    for site in fetch_radar_sites():
        if site["id"] == rid:
            return site
    return None


def supports_sr_bref(radar_id: str) -> bool:
    """Legacy helper — True only for WSR-88D sr_bref sites."""
    return preferred_product(radar_id) == "sr_bref"


# Re-export for callers
__all__ = [
    "fetch_radar_sites",
    "get_radar",
    "supports_sr_bref",
    "supports_archiving",
    "preferred_product",
]
