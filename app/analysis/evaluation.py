"""Honest nowcast evaluation with leakage-resistant splits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

import numpy as np

from app.analysis.provenance import provenance, sha256_array
from app.analysis.reflectivity import NODATA, UNKNOWN


@dataclass(frozen=True)
class EvalSplit:
    name: str
    indices: tuple[int, ...]
    source_hashes: tuple[str, ...]


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def binary_at_threshold(field: np.ndarray, threshold_bin: int) -> np.ndarray:
    return (field >= threshold_bin) & (field != UNKNOWN) & (field != NODATA)


def contingency(pred: np.ndarray, obs: np.ndarray, *, threshold_bin: int) -> dict[str, float]:
    p = binary_at_threshold(pred, threshold_bin)
    o = binary_at_threshold(obs, threshold_bin)
    # Valid domain: either side has data or both NODATA (still comparable as empty).
    hits = float(np.sum(p & o))
    misses = float(np.sum(~p & o))
    false_alarms = float(np.sum(p & ~o))
    correct_neg = float(np.sum(~p & ~o))
    precision = hits / (hits + false_alarms) if (hits + false_alarms) else 0.0
    recall = hits / (hits + misses) if (hits + misses) else 0.0
    csi = hits / (hits + misses + false_alarms) if (hits + misses + false_alarms) else 0.0
    iou = csi  # identical definition for binary fields
    return {
        "threshold_bin": float(threshold_bin),
        "hits": hits,
        "misses": misses,
        "false_alarms": false_alarms,
        "correct_negatives": correct_neg,
        "precision": precision,
        "recall": recall,
        "csi": csi,
        "iou": iou,
    }


def displacement_error_px(pred: np.ndarray, obs: np.ndarray, *, threshold_bin: int) -> float | None:
    """Centroid displacement (pixels) between binary fields; None if either empty."""
    p = binary_at_threshold(pred, threshold_bin)
    o = binary_at_threshold(obs, threshold_bin)
    if not np.any(p) or not np.any(o):
        return None
    py, px = np.nonzero(p)
    oy, ox = np.nonzero(o)
    return float(np.hypot(py.mean() - oy.mean(), px.mean() - ox.mean()))


def evaluate_nowcast(
    prediction: np.ndarray,
    observation: np.ndarray,
    *,
    thresholds: Sequence[int] = (3, 6, 9),
    prediction_hash: str | None = None,
    observation_hash: str | None = None,
    gap_flags: Sequence[str] = (),
) -> dict[str, Any]:
    """
    Report CSI/IoU, precision, recall, and displacement error at documented thresholds.
    """
    pred = np.asarray(prediction, dtype=np.int16)
    obs = np.asarray(observation, dtype=np.int16)
    if pred.shape != obs.shape:
        raise ValueError("prediction and observation shapes must match")

    by_thr = []
    for thr in thresholds:
        stats = contingency(pred, obs, threshold_bin=int(thr))
        disp = displacement_error_px(pred, obs, threshold_bin=int(thr))
        stats["displacement_error_px"] = disp
        by_thr.append(stats)

    return {
        "experimental": True,
        "disclaimer": (
            "Experimental reflectivity-only evaluation. "
            "Not a severe-weather forecast skill claim."
        ),
        "thresholds": by_thr,
        "gap_flags": list(gap_flags),
        "provenance": provenance(
            kind="nowcast_evaluation",
            source_hashes=[
                h
                for h in [
                    prediction_hash or sha256_array(pred),
                    observation_hash or sha256_array(obs),
                ]
                if h
            ],
            parameters={"thresholds": list(thresholds)},
        ),
    }


def split_by_time_blocks(
    timestamps: Sequence[Any],
    source_hashes: Sequence[str],
    *,
    block_minutes: float = 60.0,
    tune_ratio: float = 0.3,
) -> dict[str, EvalSplit]:
    """
    Split by separated time blocks (complete weather-event style), never by
    adjacent random frames. Refuses overlapping source hashes across splits.
    """
    if len(timestamps) != len(source_hashes):
        raise ValueError("timestamps and source_hashes length mismatch")
    if len(timestamps) < 2:
        raise ValueError("need at least two samples to split")

    times = [_parse_time(t) for t in timestamps]
    order = sorted(range(len(times)), key=lambda i: times[i])
    times = [times[i] for i in order]
    hashes = [source_hashes[i] for i in order]
    indices = list(order)

    # Group into contiguous blocks separated by gaps >= block_minutes.
    blocks: list[list[int]] = [[0]]
    for i in range(1, len(times)):
        gap = (times[i] - times[i - 1]).total_seconds() / 60.0
        if gap >= block_minutes:
            blocks.append([i])
        else:
            blocks[-1].append(i)

    # If only one contiguous block, split chronologically into early/late halves
    # (still time-ordered, not random adjacent shuffle).
    if len(blocks) == 1:
        mid = max(1, int(len(indices) * (1.0 - tune_ratio)))
        train_pos = list(range(0, mid))
        eval_pos = list(range(mid, len(indices)))
        if not eval_pos:
            raise ValueError("time block too small to form train/eval split")
        groups = {"train": train_pos, "eval": eval_pos}
    else:
        n_eval = max(1, int(round(len(blocks) * tune_ratio)))
        eval_blocks = blocks[-n_eval:]
        train_blocks = blocks[:-n_eval] or blocks[:1]
        if not train_blocks:
            raise ValueError("could not form a non-empty train split")
        groups = {
            "train": [i for b in train_blocks for i in b],
            "eval": [i for b in eval_blocks for i in b],
        }

    splits: dict[str, EvalSplit] = {}
    seen: dict[str, str] = {}
    for name, positions in groups.items():
        split_indices = tuple(indices[p] for p in positions)
        split_hashes = tuple(hashes[p] for p in positions)
        for h in split_hashes:
            if h in seen and seen[h] != name:
                raise ValueError(
                    f"Overlapping source hash {h} across splits {seen[h]!r} and {name!r}"
                )
            seen[h] = name
        splits[name] = EvalSplit(name=name, indices=split_indices, source_hashes=split_hashes)

    # Explicit overlap check across all pairs.
    names = list(splits)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a = set(splits[names[i]].source_hashes)
            b = set(splits[names[j]].source_hashes)
            overlap = a & b
            if overlap:
                raise ValueError(
                    f"Overlapping source hashes across {names[i]} and {names[j]}: {sorted(overlap)}"
                )
    return splits


def assert_hash_disjoint(*hash_sets: Sequence[str]) -> None:
    seen: dict[str, int] = {}
    for idx, hs in enumerate(hash_sets):
        for h in hs:
            if h in seen:
                raise ValueError(
                    f"Overlapping source hash {h} between split {seen[h]} and {idx}"
                )
            seen[h] = idx
