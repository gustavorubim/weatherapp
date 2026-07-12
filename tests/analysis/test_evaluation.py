from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from app.analysis.evaluation import (
    assert_hash_disjoint,
    evaluate_nowcast,
    split_by_time_blocks,
)
from app.analysis.fixtures import synthetic_moving_cell
from app.analysis.provenance import sha256_array


def test_evaluate_reports_csi_precision_recall_displacement():
    pred = np.full((40, 40), -1, dtype=np.int16)
    obs = np.full((40, 40), -1, dtype=np.int16)
    pred[10:16, 10:16] = 5
    obs[10:16, 12:18] = 5  # shifted
    result = evaluate_nowcast(pred, obs, thresholds=(5,))
    stats = result["thresholds"][0]
    assert "csi" in stats and "iou" in stats
    assert "precision" in stats and "recall" in stats
    assert stats["displacement_error_px"] is not None
    assert stats["displacement_error_px"] > 0
    assert result["experimental"] is True


def test_split_by_time_blocks_not_random_adjacent():
    t0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    # Two weather events separated by 3 hours.
    timestamps = [
        t0,
        t0 + timedelta(minutes=5),
        t0 + timedelta(minutes=10),
        t0 + timedelta(hours=3),
        t0 + timedelta(hours=3, minutes=5),
    ]
    hashes = [f"h{i}" for i in range(5)]
    splits = split_by_time_blocks(timestamps, hashes, block_minutes=60.0, tune_ratio=0.5)
    assert "train" in splits and "eval" in splits
    train_h = set(splits["train"].source_hashes)
    eval_h = set(splits["eval"].source_hashes)
    assert train_h.isdisjoint(eval_h)


def test_evaluation_refuses_overlapping_hashes():
    t0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    timestamps = [t0 + timedelta(minutes=5 * i) for i in range(4)]
    # Duplicate hash across intended chronological halves.
    hashes = ["a", "b", "a", "c"]
    with pytest.raises(ValueError, match="Overlapping source hash"):
        split_by_time_blocks(timestamps, hashes, block_minutes=1000.0, tune_ratio=0.5)


def test_assert_hash_disjoint():
    assert_hash_disjoint(["a", "b"], ["c"])
    with pytest.raises(ValueError):
        assert_hash_disjoint(["a", "b"], ["b", "c"])


def test_gaps_surfaced_in_metrics():
    pred = np.zeros((8, 8), dtype=np.int16)
    obs = np.zeros((8, 8), dtype=np.int16)
    result = evaluate_nowcast(
        pred,
        obs,
        thresholds=(1,),
        gap_flags=("large_gap_90.0min_between_a_and_b",),
    )
    assert result["gap_flags"]
    assert "large_gap" in result["gap_flags"][0]


def test_fixture_hashes_unique():
    fix = synthetic_moving_cell(n_frames=4)
    assert len(set(fix["source_hashes"])) == 4
    assert all(sha256_array(b) == h for b, h in zip(fix["frames_bins"], fix["source_hashes"]))
