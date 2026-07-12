from __future__ import annotations

import numpy as np

from app.analysis.clutter import build_clutter_frequency
from app.analysis.fixtures import synthetic_static_clutter


def test_clutter_frequency_mask_not_source_rewrite():
    fix = synthetic_static_clutter(n_frames=10)
    originals = [b.copy() for b in fix["frames_bins"]]
    result = build_clutter_frequency(
        [{"bins": b} for b in fix["frames_bins"]],
        min_presence=0.8,
        min_bin=0,
    )
    # Sources unchanged.
    for a, b in zip(originals, fix["frames_bins"]):
        assert np.array_equal(a, b)

    assert result.mask.dtype == bool
    assert "clutter_pixels" in result.metrics
    assert result.provenance["kind"] == "clutter_frequency"
    assert result.provenance["source_hashes"]
    assert "min_presence" in result.provenance["parameters"]

    cy, cx = fix["clutter_yx"]
    assert result.mask[cy, cx]
    # Storm region should not be fully clutter (moves away).
    assert result.metrics["coverage_fraction"] < 0.5


def test_clutter_threshold_configurable():
    fix = synthetic_static_clutter(n_frames=8)
    loose = build_clutter_frequency(
        [{"bins": b} for b in fix["frames_bins"]], min_presence=0.5
    )
    strict = build_clutter_frequency(
        [{"bins": b} for b in fix["frames_bins"]], min_presence=0.95
    )
    assert strict.metrics["clutter_pixels"] <= loose.metrics["clutter_pixels"]
