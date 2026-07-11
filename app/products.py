from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from app.config import DATA_DIR, USER_AGENT, WMS_OWS, ensure_dirs

logger = logging.getLogger(__name__)

# Prefer higher-quality / standard products first.
PRODUCT_PREFERENCE = ("sr_bref", "bref1", "brefl")

# Approximate coverage radii used for GetMap bbox.
RANGE_BY_PRODUCT_M = {
    "sr_bref": 230_000,  # WSR-88D
    "bref1": 90_000,  # TDWR
    "brefl": 90_000,
    "bvel": 90_000,
}

_PRODUCT_INDEX: dict[str, dict[str, Any]] | None = None


def range_for_product(product: str) -> float:
    return float(RANGE_BY_PRODUCT_M.get(product, 230_000))


def discover_product_index(*, force_refresh: bool = False) -> dict[str, dict[str, Any]]:
    """
    Map lowercase ICAO -> available reflectivity-like products from WMS capabilities.

    Example entry:
      {"products": ["bref1", "brefl", "bvel"], "preferred": "bref1"}
    """
    global _PRODUCT_INDEX
    if _PRODUCT_INDEX is not None and not force_refresh:
        return dict(_PRODUCT_INDEX)

    cache_path = DATA_DIR / "product_index.json"
    if not force_refresh and cache_path.exists():
        try:
            data = json.loads(cache_path.read_text())
            if isinstance(data, dict) and data:
                _PRODUCT_INDEX = data
                return dict(data)
        except (json.JSONDecodeError, OSError):
            pass

    url = f"{WMS_OWS}?service=WMS&version=1.3.0&request=GetCapabilities"
    index: dict[str, dict[str, Any]] = {}
    try:
        with httpx.Client(timeout=120.0, headers={"User-Agent": USER_AGENT}, trust_env=False) as client:
            resp = client.get(url)
            resp.raise_for_status()
            text = resp.text
        names = re.findall(r"<Name>([^<]+)</Name>", text)
        by_id: dict[str, set[str]] = {}
        for name in names:
            part = name.split(":")[-1].lower()
            # icao_product e.g. ktbw_sr_bref, tmco_bref1
            if "_" not in part:
                continue
            icao, product = part.split("_", 1)
            if len(icao) < 3:
                continue
            by_id.setdefault(icao, set()).add(product)

        for icao, products in by_id.items():
            preferred = next((p for p in PRODUCT_PREFERENCE if p in products), None)
            if not preferred:
                continue
            index[icao] = {
                "products": sorted(products),
                "preferred": preferred,
                "kind": "wsr88d" if preferred == "sr_bref" else "tdwr",
            }

        if index:
            ensure_dirs()
            cache_path.write_text(json.dumps(index, indent=2, sort_keys=True))
            _PRODUCT_INDEX = index
            logger.info("Discovered archive products for %d radars", len(index))
            return dict(index)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not build product index from WMS capabilities: %s", exc)

    _PRODUCT_INDEX = {}
    return {}


def preferred_product(radar_id: str) -> str | None:
    entry = discover_product_index().get(radar_id.strip().lower())
    if not entry:
        return None
    return entry.get("preferred")


def supports_archiving(radar_id: str) -> bool:
    return preferred_product(radar_id) is not None


def product_info(radar_id: str) -> dict[str, Any] | None:
    return discover_product_index().get(radar_id.strip().lower())
