"""Clutter-frequency estimation from historical reflectivity frames.

Produces a mask and metrics report. Never rewrites raw source imagery.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np
from PIL import Image

from app.analysis.provenance import provenance, sha256_array, sha256_bytes
from app.analysis.reflectivity import NODATA, UNKNOWN, decode_reflectivity_bins


@dataclass(frozen=True)
class ClutterResult:
    mask: np.ndarray  # bool, True where clutter frequency >= min_presence
    frequency: np.ndarray  # float32 in [0, 1]
    metrics: dict[str, Any]
    provenance: dict[str, Any]
    # Optional overlay — derived only; sources untouched
    mask_rgba: np.ndarray | None = None


def _frame_to_bins(frame: Any) -> tuple[np.ndarray, str]:
    if isinstance(frame, dict):
        if "bins" in frame:
            bins = np.asarray(frame["bins"], dtype=np.int16)
            src = frame.get("source_hash") or sha256_array(bins)
            return bins, str(src)
        if "path" in frame:
            data = open(frame["path"], "rb").read()
            bins = decode_reflectivity_bins(data)
            assert isinstance(bins, np.ndarray)
            return bins, sha256_bytes(data)
        if "image" in frame:
            img = frame["image"]
            bins = decode_reflectivity_bins(img)
            assert isinstance(bins, np.ndarray)
            if isinstance(img, Image.Image):
                return bins, sha256_bytes(img.convert("RGBA").tobytes())
            return bins, sha256_array(np.asarray(img))
        raise ValueError("frame dict must contain bins, path, or image")
    if isinstance(frame, (str,)):
        data = open(frame, "rb").read()
        bins = decode_reflectivity_bins(data)
        assert isinstance(bins, np.ndarray)
        return bins, sha256_bytes(data)
    if isinstance(frame, Image.Image):
        bins = decode_reflectivity_bins(frame)
        assert isinstance(bins, np.ndarray)
        return bins, sha256_bytes(frame.convert("RGBA").tobytes())
    if isinstance(frame, np.ndarray) and frame.ndim == 2:
        return frame.astype(np.int16), sha256_array(frame)
    if isinstance(frame, (bytes, bytearray)):
        bins = decode_reflectivity_bins(bytes(frame))
        assert isinstance(bins, np.ndarray)
        return bins, sha256_bytes(bytes(frame))
    raise TypeError(f"Unsupported frame type: {type(frame)!r}")


def build_clutter_frequency(
    frames: Sequence[Any],
    *,
    min_presence: float = 0.8,
    min_bin: int = 0,
) -> ClutterResult:
    """
    Estimate persistent clutter as pixels present in >= min_presence of frames.

    Presence means a valid reflectivity bin >= min_bin (NODATA/UNKNOWN ignored).
    """
    if not frames:
        raise ValueError("frames must be non-empty")
    if not (0.0 < min_presence <= 1.0):
        raise ValueError("min_presence must be in (0, 1]")

    decoded: list[np.ndarray] = []
    hashes: list[str] = []
    for frame in frames:
        bins, h = _frame_to_bins(frame)
        decoded.append(bins)
        hashes.append(h)

    shape = decoded[0].shape
    for b in decoded:
        if b.shape != shape:
            raise ValueError(f"Inconsistent frame shapes: {shape} vs {b.shape}")

    present = np.zeros(shape, dtype=np.float32)
    for bins in decoded:
        valid = (bins >= min_bin) & (bins != UNKNOWN) & (bins != NODATA)
        present += valid.astype(np.float32)

    n = float(len(decoded))
    frequency = present / n
    mask = frequency >= float(min_presence)

    mask_rgba = np.zeros((*shape, 4), dtype=np.uint8)
    mask_rgba[mask] = (255, 64, 0, 160)

    metrics = {
        "frame_count": len(decoded),
        "min_presence": float(min_presence),
        "min_bin": int(min_bin),
        "clutter_pixels": int(np.sum(mask)),
        "mean_frequency": float(np.mean(frequency)),
        "max_frequency": float(np.max(frequency)),
        "coverage_fraction": float(np.mean(mask)),
    }
    prov = provenance(
        kind="clutter_frequency",
        source_hashes=hashes,
        parameters={
            "min_presence": float(min_presence),
            "min_bin": int(min_bin),
            "frame_count": len(decoded),
        },
        notes="Clutter output is a mask and metrics report; raw frames are not modified.",
    )
    return ClutterResult(
        mask=mask,
        frequency=frequency.astype(np.float32),
        metrics=metrics,
        provenance=prov,
        mask_rgba=mask_rgba,
    )
