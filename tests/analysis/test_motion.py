from __future__ import annotations

from datetime import timedelta

import numpy as np

from app.analysis.fixtures import synthetic_moving_cell
from app.analysis.motion import estimate_motion


def test_motion_uses_timestamps_not_fixed_cadence():
    fix = synthetic_moving_cell(
        size=64,
        n_frames=4,
        radius=6,
        velocity_yx=(0.0, 2.0),
        dt_minutes=5.0,
    )
    # Uneven timestamps: stretch middle gap.
    timestamps = list(fix["timestamps"])
    timestamps[2] = timestamps[1] + timedelta(minutes=15)
    timestamps[3] = timestamps[2] + timedelta(minutes=5)

    motion = estimate_motion(
        fix["frames_bins"],
        timestamps,
        max_gap_minutes=20.0,
    )
    assert motion.method
    assert motion.provenance["parameters"]["timestamps"]
    # Mean motion should be roughly eastward (+u).
    assert motion.mean_u_px_per_hour > 0


def test_large_gaps_are_surfaced():
    fix = synthetic_moving_cell(size=48, n_frames=3, velocity_yx=(0.0, 1.0), dt_minutes=5.0)
    timestamps = list(fix["timestamps"])
    timestamps[2] = timestamps[1] + timedelta(minutes=90)
    motion = estimate_motion(fix["frames_bins"], timestamps, max_gap_minutes=20.0)
    assert motion.gap_flags
    assert any("large_gap" in g for g in motion.gap_flags)


def test_motion_direction_on_synthetic():
    fix = synthetic_moving_cell(
        size=80,
        n_frames=5,
        radius=5,
        velocity_yx=(0.0, 2.0),
        dt_minutes=5.0,
        start_yx=(40.0, 15.0),
    )
    motion = estimate_motion(fix["frames_bins"], fix["timestamps"])
    # 2 px / 5 min => 24 px/hour
    assert abs(motion.mean_u_px_per_hour - 24.0) < 6.0
    assert abs(motion.mean_v_px_per_hour) < 6.0
