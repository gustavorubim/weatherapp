"""Reflectivity color → ordinal bin decoding.

Maps archived WMS RGBA radar imagery to documented ordinal reflectivity bins.
This is an approximate palette decode for analysis — not calibrated Level-II dBZ.
Unknown opaque colors and transparent pixels are handled explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image

from app.analysis.provenance import provenance, sha256_array, sha256_bytes

# Special bin codes (never treated as precipitation intensity).
NODATA = -1  # transparent / missing echo
UNKNOWN = -2  # opaque color not matching the documented palette

# Documented ordinal bins for a classic NWS-like base-reflectivity palette.
# approx_dbz is the representative midpoint used for reporting only.
# RGB anchors are the colors painted by synthetic fixtures and nearest-matched
# against real NOAA opengeo WMS-style imagery (tolerance applied at decode time).
PALETTE_ENTRIES: list[tuple[int, int, tuple[int, int, int]]] = [
    # bin, approx_dbz, rgb
    (0, 5, (4, 233, 231)),
    (1, 10, (1, 159, 244)),
    (2, 15, (3, 0, 244)),
    (3, 20, (2, 144, 2)),
    (4, 25, (1, 200, 1)),
    (5, 30, (0, 232, 0)),
    (6, 35, (255, 255, 0)),
    (7, 40, (231, 192, 0)),
    (8, 45, (255, 144, 0)),
    (9, 50, (255, 0, 0)),
    (10, 55, (214, 0, 0)),
    (11, 60, (192, 0, 0)),
    (12, 65, (255, 0, 255)),
    (13, 70, (153, 85, 201)),
    (14, 75, (255, 255, 255)),
]

PALETTE_NAME = "nws_like_bref_v1"
DEFAULT_MATCH_TOLERANCE = 48.0  # max RGB L2 distance to accept a palette match


@dataclass(frozen=True)
class PaletteEntry:
    bin: int
    approx_dbz: int
    rgb: tuple[int, int, int]


def documented_palette() -> list[PaletteEntry]:
    return [PaletteEntry(b, dbz, rgb) for b, dbz, rgb in PALETTE_ENTRIES]


def bin_to_approx_dbz(bin_value: int) -> int | None:
    for b, dbz, _ in PALETTE_ENTRIES:
        if b == bin_value:
            return dbz
    return None


def _palette_rgb_array() -> np.ndarray:
    return np.asarray([rgb for _, _, rgb in PALETTE_ENTRIES], dtype=np.float32)


def _palette_bins_array() -> np.ndarray:
    return np.asarray([b for b, _, _ in PALETTE_ENTRIES], dtype=np.int16)


def decode_reflectivity_bins(
    image: Image.Image | np.ndarray | bytes,
    *,
    match_tolerance: float = DEFAULT_MATCH_TOLERANCE,
    return_provenance: bool = False,
) -> np.ndarray | tuple[np.ndarray, dict[str, Any]]:
    """
    Map an RGBA (or RGB) radar frame to ordinal reflectivity bins.

    Returns an int16 array with:
      - bin >= 0 : documented ordinal reflectivity bin
      - NODATA (-1): transparent / no echo
      - UNKNOWN (-2): opaque color outside the documented palette

    Raw image bytes are never modified.
    """
    source_hash: str | None = None
    if isinstance(image, (bytes, bytearray)):
        raw = bytes(image)
        source_hash = sha256_bytes(raw)
        img = Image.open(__import__("io").BytesIO(raw)).convert("RGBA")
        arr = np.asarray(img)
    elif isinstance(image, Image.Image):
        rgba = image.convert("RGBA")
        arr = np.asarray(rgba)
        source_hash = sha256_bytes(rgba.tobytes())
    else:
        arr = np.asarray(image)
        if arr.ndim == 2:
            raise ValueError("Expected RGB/RGBA image array, got single channel")
        if arr.shape[2] == 3:
            alpha = np.full(arr.shape[:2], 255, dtype=np.uint8)
            arr = np.dstack([arr.astype(np.uint8), alpha])
        elif arr.shape[2] != 4:
            raise ValueError(f"Expected 3 or 4 channels, got shape {arr.shape}")
        source_hash = sha256_array(arr)

    rgb = arr[:, :, :3].astype(np.float32)
    alpha = arr[:, :, 3]
    h, w = alpha.shape
    out = np.full((h, w), NODATA, dtype=np.int16)

    opaque = alpha >= 16
    if not np.any(opaque):
        if return_provenance:
            return out, provenance(
                kind="reflectivity_bins",
                source_hashes=[source_hash],
                parameters={
                    "palette": PALETTE_NAME,
                    "match_tolerance": match_tolerance,
                    "opaque_pixels": 0,
                    "unknown_pixels": 0,
                },
            )
        return out

    palette = _palette_rgb_array()
    bins = _palette_bins_array()
    pixels = rgb[opaque]  # (N, 3)
    # Squared distances to each palette color.
    # (N, 1, 3) - (P, 3) -> (N, P)
    diff = pixels[:, None, :] - palette[None, :, :]
    dist2 = np.sum(diff * diff, axis=2)
    nearest = np.argmin(dist2, axis=1)
    best = dist2[np.arange(dist2.shape[0]), nearest]
    tol2 = float(match_tolerance) ** 2
    matched = best <= tol2

    mapped = np.full(pixels.shape[0], UNKNOWN, dtype=np.int16)
    mapped[matched] = bins[nearest[matched]]
    out[opaque] = mapped

    if return_provenance:
        unknown_count = int(np.sum(out == UNKNOWN))
        return out, provenance(
            kind="reflectivity_bins",
            source_hashes=[source_hash],
            parameters={
                "palette": PALETTE_NAME,
                "match_tolerance": match_tolerance,
                "opaque_pixels": int(np.sum(opaque)),
                "unknown_pixels": unknown_count,
                "entries": [
                    {"bin": b, "approx_dbz": dbz, "rgb": list(rgb)}
                    for b, dbz, rgb in PALETTE_ENTRIES
                ],
            },
        )
    return out


def bins_to_rgba(bins: np.ndarray) -> np.ndarray:
    """Render ordinal bins back to RGBA using the documented palette (for overlays)."""
    h, w = bins.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    for b, _, rgb in PALETTE_ENTRIES:
        mask = bins == b
        if np.any(mask):
            rgba[mask, 0] = rgb[0]
            rgba[mask, 1] = rgb[1]
            rgba[mask, 2] = rgb[2]
            rgba[mask, 3] = 255
    # UNKNOWN shown as dim magenta outline color for debugging overlays
    unk = bins == UNKNOWN
    if np.any(unk):
        rgba[unk] = (128, 0, 128, 180)
    return rgba


def paint_bins(bins: np.ndarray) -> Image.Image:
    return Image.fromarray(bins_to_rgba(bins), mode="RGBA")
