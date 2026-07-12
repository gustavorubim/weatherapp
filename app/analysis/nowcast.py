"""Advection nowcast and persistence baseline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np
from scipy.ndimage import map_coordinates

from app.analysis.motion import MotionField, estimate_motion
from app.analysis.provenance import provenance, sha256_array
from app.analysis.reflectivity import NODATA, UNKNOWN, decode_reflectivity_bins

SUPPORTED_LEAD_MINUTES = (5, 15, 30, 60)


@dataclass(frozen=True)
class NowcastResult:
    prediction: np.ndarray  # int16 bins
    method: str  # advection | persistence
    lead_minutes: int
    provenance: dict[str, Any]
    experimental: bool = True


def _to_bins(frame: Any) -> tuple[np.ndarray, str]:
    if isinstance(frame, dict) and "bins" in frame:
        bins = np.asarray(frame["bins"], dtype=np.int16)
        return bins, frame.get("source_hash") or sha256_array(bins)
    if isinstance(frame, np.ndarray) and frame.ndim == 2:
        return frame.astype(np.int16), sha256_array(frame)
    bins = decode_reflectivity_bins(frame)
    assert isinstance(bins, np.ndarray)
    return bins, sha256_array(bins)


def persistence_nowcast(frame: Any, *, lead_minutes: int) -> NowcastResult:
    """Mandatory baseline: the future equals the last observation."""
    if lead_minutes not in SUPPORTED_LEAD_MINUTES:
        raise ValueError(
            f"lead_minutes must be one of {SUPPORTED_LEAD_MINUTES}, got {lead_minutes}"
        )
    bins, source_hash = _to_bins(frame)
    return NowcastResult(
        prediction=bins.copy(),
        method="persistence",
        lead_minutes=int(lead_minutes),
        provenance=provenance(
            kind="nowcast",
            source_hashes=[source_hash],
            parameters={"method": "persistence", "lead_minutes": int(lead_minutes)},
            notes=(
                "Experimental reflectivity-only nowcast baseline. "
                "Does not claim severe-weather prediction."
            ),
        ),
        experimental=True,
    )


def advect_nowcast(
    frame: Any,
    motion: MotionField,
    *,
    lead_minutes: int,
) -> NowcastResult:
    """
    Advect a reflectivity field by the estimated motion for lead_minutes.

    Supports lead times 5, 15, 30, and 60 minutes. Output is labeled experimental.
    """
    if lead_minutes not in SUPPORTED_LEAD_MINUTES:
        raise ValueError(
            f"lead_minutes must be one of {SUPPORTED_LEAD_MINUTES}, got {lead_minutes}"
        )
    bins, source_hash = _to_bins(frame)
    if bins.shape != motion.u_px_per_hour.shape:
        raise ValueError("frame shape must match motion field shape")

    hours = float(lead_minutes) / 60.0
    h, w = bins.shape
    yy, xx = np.meshgrid(np.arange(h, dtype=np.float64), np.arange(w, dtype=np.float64), indexing="ij")
    # Source coordinates for each destination pixel: dest = source + displacement,
    # so sample at dest - displacement.
    src_y = yy - motion.v_px_per_hour * hours
    src_x = xx - motion.u_px_per_hour * hours

    # Nearest-neighbor sampling preserves ordinal bins.
    sampled = map_coordinates(
        bins.astype(np.float64),
        [src_y.ravel(), src_x.ravel()],
        order=0,
        mode="constant",
        cval=float(NODATA),
    )
    prediction = sampled.reshape(h, w).astype(np.int16)
    # Preserve UNKNOWN where the source had UNKNOWN and we landed on it.
    prediction[(prediction != NODATA) & (prediction < 0) & (prediction != UNKNOWN)] = NODATA

    source_hashes = [source_hash] + list(motion.provenance.get("source_hashes", []))
    return NowcastResult(
        prediction=prediction,
        method="advection",
        lead_minutes=int(lead_minutes),
        provenance=provenance(
            kind="nowcast",
            source_hashes=source_hashes,
            parameters={
                "method": "advection",
                "lead_minutes": int(lead_minutes),
                "motion_method": motion.method,
                "mean_u_px_per_hour": motion.mean_u_px_per_hour,
                "mean_v_px_per_hour": motion.mean_v_px_per_hour,
                "gap_flags": list(motion.gap_flags),
            },
            notes=(
                "Experimental reflectivity-only advection nowcast. "
                "Does not claim severe-weather prediction. "
                "Reflectivity imagery alone cannot support rotation or tornado inference."
            ),
        ),
        experimental=True,
    )


def nowcast_from_frames(
    frames: Sequence[Any],
    timestamps: Sequence[Any],
    *,
    lead_minutes: int = 15,
    method: str = "advection",
) -> NowcastResult:
    """Convenience: estimate motion from frames then nowcast the last frame."""
    if method == "persistence":
        return persistence_nowcast(frames[-1], lead_minutes=lead_minutes)
    if method != "advection":
        raise ValueError("method must be 'advection' or 'persistence'")
    motion = estimate_motion(frames[:-1] + [frames[-1]], timestamps)
    return advect_nowcast(frames[-1], motion, lead_minutes=lead_minutes)
