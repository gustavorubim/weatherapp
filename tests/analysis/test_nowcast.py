from __future__ import annotations

import numpy as np

from app.analysis.fixtures import synthetic_moving_cell
from app.analysis.motion import estimate_motion
from app.analysis.nowcast import SUPPORTED_LEAD_MINUTES, advect_nowcast, persistence_nowcast
from app.analysis.evaluation import evaluate_nowcast


def test_supported_lead_times():
    fix = synthetic_moving_cell(size=48, n_frames=3)
    motion = estimate_motion(fix["frames_bins"][:2], fix["timestamps"][:2])
    for lead in SUPPORTED_LEAD_MINUTES:
        result = advect_nowcast(fix["frames_bins"][1], motion, lead_minutes=lead)
        assert result.lead_minutes == lead
        assert result.experimental is True
        assert "Experimental" in result.provenance["disclaimer"] or result.provenance.get(
            "experimental"
        )
        pers = persistence_nowcast(fix["frames_bins"][1], lead_minutes=lead)
        assert pers.method == "persistence"
        assert np.array_equal(pers.prediction, fix["frames_bins"][1])


def test_advection_outperforms_persistence_on_synthetic():
    fix = synthetic_moving_cell(
        size=96,
        n_frames=6,
        radius=6,
        bin_value=8,
        dt_minutes=5.0,
        velocity_yx=(0.0, 2.0),
        start_yx=(48.0, 20.0),
    )
    lead = int(fix["documented_lead_minutes"])
    motion = estimate_motion(fix["frames_bins"][:-1], fix["timestamps"][:-1])
    adv = advect_nowcast(fix["frames_bins"][-2], motion, lead_minutes=lead)
    pers = persistence_nowcast(fix["frames_bins"][-2], lead_minutes=lead)
    obs = fix["frames_bins"][-1]
    thr = 8
    adv_m = evaluate_nowcast(adv.prediction, obs, thresholds=(thr,))
    pers_m = evaluate_nowcast(pers.prediction, obs, thresholds=(thr,))
    assert adv_m["thresholds"][0]["csi"] > pers_m["thresholds"][0]["csi"]


def test_nowcast_rejects_unsupported_lead():
    fix = synthetic_moving_cell(size=32, n_frames=2)
    motion = estimate_motion(fix["frames_bins"], fix["timestamps"])
    try:
        advect_nowcast(fix["frames_bins"][-1], motion, lead_minutes=7)
        assert False, "expected ValueError"
    except ValueError:
        pass
