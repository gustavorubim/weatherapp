"""Synthetic analysis fixtures (deterministic, no external data)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

from app.analysis.reflectivity import PALETTE_ENTRIES, paint_bins
from app.analysis.provenance import sha256_array


def synthetic_moving_cell(
    *,
    size: int = 128,
    radius: int = 8,
    bin_value: int = 8,
    n_frames: int = 6,
    dt_minutes: float = 5.0,
    velocity_yx: tuple[float, float] = (0.0, 2.0),  # (vy, vx) px per frame
    start_yx: tuple[float, float] = (40.0, 20.0),
) -> dict[str, Any]:
    """
    Translating disk of constant reflectivity.

    Documented lead time for advection-vs-persistence acceptance: dt_minutes
    (one-step) and multi-step lead = dt_minutes * steps.
    """
    if bin_value < 0 or bin_value >= len(PALETTE_ENTRIES):
        raise ValueError("bin_value out of palette range")

    frames_bins: list[np.ndarray] = []
    timestamps: list[datetime] = []
    t0 = datetime(2026, 7, 11, 21, 0, 0, tzinfo=timezone.utc)
    hashes: list[str] = []

    vy, vx = velocity_yx
    sy, sx = start_yx
    yy, xx = np.ogrid[:size, :size]

    for i in range(n_frames):
        cy = sy + i * vy
        cx = sx + i * vx
        bins = np.full((size, size), -1, dtype=np.int16)  # NODATA
        mask = (yy - cy) ** 2 + (xx - cx) ** 2 <= radius**2
        bins[mask] = bin_value
        frames_bins.append(bins)
        hashes.append(sha256_array(bins))
        timestamps.append(t0 + timedelta(minutes=dt_minutes * i))

    images = [paint_bins(b) for b in frames_bins]
    return {
        "name": "synthetic-moving-cell",
        "documented_lead_minutes": int(dt_minutes),
        "velocity_yx_per_frame": velocity_yx,
        "dt_minutes": dt_minutes,
        "km_per_pixel": 0.225,
        "frames_bins": frames_bins,
        "images": images,
        "timestamps": timestamps,
        "source_hashes": hashes,
        "notes": (
            "Advection must outperform persistence at documented_lead_minutes "
            "on this translating-cell fixture."
        ),
    }


def synthetic_static_clutter(
    *,
    size: int = 64,
    n_frames: int = 10,
    clutter_yx: tuple[int, int] = (10, 10),
    clutter_radius: int = 3,
    storm_start: tuple[float, float] = (40.0, 10.0),
    storm_velocity: tuple[float, float] = (0.0, 3.0),
    storm_radius: int = 5,
    storm_bin: int = 7,
    clutter_bin: int = 3,
) -> dict[str, Any]:
    """Persistent clutter blob plus a translating storm."""
    frames: list[np.ndarray] = []
    yy, xx = np.ogrid[:size, :size]
    cy0, cx0 = clutter_yx
    clutter_mask = (yy - cy0) ** 2 + (xx - cx0) ** 2 <= clutter_radius**2
    t0 = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
    timestamps = []
    for i in range(n_frames):
        bins = np.full((size, size), -1, dtype=np.int16)
        bins[clutter_mask] = clutter_bin
        sy = storm_start[0] + i * storm_velocity[0]
        sx = storm_start[1] + i * storm_velocity[1]
        storm = (yy - sy) ** 2 + (xx - sx) ** 2 <= storm_radius**2
        bins[storm] = storm_bin
        frames.append(bins)
        timestamps.append(t0 + timedelta(minutes=5 * i))
    return {
        "name": "synthetic-static-clutter",
        "frames_bins": frames,
        "timestamps": timestamps,
        "clutter_yx": clutter_yx,
    }
