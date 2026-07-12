"""RadarVault analysis primitives (WT6) — offline, reproducible, read-only on raw frames."""

from __future__ import annotations

from app.analysis.cells import Cell, Track, detect_cells, track_cells
from app.analysis.clutter import ClutterResult, build_clutter_frequency
from app.analysis.evaluation import evaluate_nowcast, split_by_time_blocks
from app.analysis.motion import MotionField, estimate_motion
from app.analysis.nowcast import NowcastResult, advect_nowcast, nowcast_from_frames, persistence_nowcast
from app.analysis.reflectivity import (
    NODATA,
    UNKNOWN,
    decode_reflectivity_bins,
    documented_palette,
)

__all__ = [
    "NODATA",
    "UNKNOWN",
    "Cell",
    "ClutterResult",
    "MotionField",
    "NowcastResult",
    "Track",
    "advect_nowcast",
    "build_clutter_frequency",
    "decode_reflectivity_bins",
    "detect_cells",
    "documented_palette",
    "estimate_motion",
    "evaluate_nowcast",
    "persistence_nowcast",
    "nowcast_from_frames",
    "split_by_time_blocks",
    "track_cells",
]
