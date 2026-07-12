from __future__ import annotations

import numpy as np

from app.analysis.cells import detect_cells, track_cells
from app.analysis.fixtures import synthetic_moving_cell


def test_detect_cells_fields():
    fix = synthetic_moving_cell(size=64, n_frames=1, radius=6, bin_value=8)
    cells = detect_cells(fix["frames_bins"][0], min_bin=4, min_pixels=10)
    assert len(cells) == 1
    cell = cells[0]
    assert cell.area_pixels >= 10
    assert cell.max_bin == 8
    assert abs(cell.mean_bin - 8) < 1e-6
    y0, x0, y1, x1 = cell.bbox_yx
    assert y1 > y0 and x1 > x0
    cy, cx = cell.centroid_yx
    assert y0 <= cy < y1 and x0 <= cx < x1


def test_track_moving_cell_direction_and_speed():
    # Move east (+x) at 2 px / 5 min, km_per_pixel=0.225
    # speed = 2 px / (5/60) h * 0.225 km/px = 2 * 12 * 0.225 = 5.4 km/h
    fix = synthetic_moving_cell(
        size=80,
        n_frames=5,
        radius=5,
        bin_value=7,
        dt_minutes=5.0,
        velocity_yx=(0.0, 2.0),
        start_yx=(40.0, 10.0),
    )
    stamped = []
    for bins, ts in zip(fix["frames_bins"], fix["timestamps"]):
        cells = detect_cells(bins, min_bin=4, min_pixels=8)
        stamped.append((ts, cells))
    tracks = track_cells(stamped, max_speed_kmh=120.0, km_per_pixel=0.225)
    assert tracks
    primary = max(tracks, key=lambda t: len(t.points))
    assert len(primary.points) >= 4
    assert primary.speed_kmh is not None
    assert abs(primary.speed_kmh - 5.4) < 1.0
    # Eastward ≈ 90°
    assert primary.direction_deg is not None
    assert abs((primary.direction_deg - 90.0 + 180) % 360 - 180) < 25


def test_tracks_handle_birth_death_merge_split_without_crash():
    # Frame 0: one cell. Frame 1: two cells (split). Frame 2: none (death).
    size = 64
    frames = []
    for i in range(3):
        bins = np.full((size, size), -1, dtype=np.int16)
        if i == 0:
            bins[20:28, 20:28] = 6
        elif i == 1:
            bins[18:24, 18:24] = 6
            bins[30:36, 40:46] = 6
        frames.append(bins)
    from datetime import datetime, timedelta, timezone

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    stamped = [
        (t0 + timedelta(minutes=5 * i), detect_cells(frames[i], min_bin=4, min_pixels=4))
        for i in range(3)
    ]
    tracks = track_cells(stamped, max_speed_kmh=500.0, km_per_pixel=1.0)
    assert isinstance(tracks, list)
    # At least one birth/death note path exercised.
    statuses = {t.status for t in tracks}
    assert statuses  # non-empty; no crash
