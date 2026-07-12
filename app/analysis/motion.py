"""Motion-field estimation from ordered reflectivity frames."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

import numpy as np
from scipy import ndimage

from app.analysis.provenance import provenance, sha256_array
from app.analysis.reflectivity import NODATA, UNKNOWN, decode_reflectivity_bins


@dataclass(frozen=True)
class MotionField:
    """Pixel displacement per hour (image coordinates: +u right, +v down)."""

    u_px_per_hour: np.ndarray
    v_px_per_hour: np.ndarray
    confidence: np.ndarray
    method: str
    provenance: dict[str, Any]
    mean_u_px_per_hour: float
    mean_v_px_per_hour: float
    gap_flags: tuple[str, ...] = ()


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


def _to_bins(frame: Any) -> tuple[np.ndarray, str]:
    if isinstance(frame, dict) and "bins" in frame:
        bins = np.asarray(frame["bins"], dtype=np.int16)
        return bins, frame.get("source_hash") or sha256_array(bins)
    if isinstance(frame, np.ndarray) and frame.ndim == 2:
        return frame.astype(np.int16), sha256_array(frame)
    bins = decode_reflectivity_bins(frame)
    assert isinstance(bins, np.ndarray)
    return bins, sha256_array(bins)


def _intensity(bins: np.ndarray) -> np.ndarray:
    out = bins.astype(np.float32)
    out[(bins < 0) | (bins == UNKNOWN) | (bins == NODATA)] = 0.0
    return out


def _phase_correlation_shift(a: np.ndarray, b: np.ndarray) -> tuple[float, float, float]:
    """
    Return (dy, dx, peak) shifting a → b via phase correlation.
    Positive dy moves content downward; positive dx moves right.
    """
    fa = np.fft.fft2(a)
    fb = np.fft.fft2(b)
    cross = fa * np.conj(fb)
    mag = np.abs(cross)
    mag[mag < 1e-12] = 1e-12
    r = np.fft.ifft2(cross / mag)
    r = np.real(r)
    peak_idx = np.unravel_index(np.argmax(r), r.shape)
    peak = float(r[peak_idx])
    dy = float(peak_idx[0])
    dx = float(peak_idx[1])
    h, w = a.shape
    if dy > h / 2:
        dy -= h
    if dx > w / 2:
        dx -= w
    # Phase correlation of a vs b gives shift that aligns a onto b when applied to a.
    # Convention: displacement of features from a to b is (-dy, -dx) of the peak
    # for the standard "shift of a to match b". We want feature motion a→b = -peak.
    return -dy, -dx, peak


def estimate_motion(
    frames: Sequence[Any],
    timestamps: Sequence[Any],
    *,
    max_gap_minutes: float = 20.0,
    min_bin: int = 0,
) -> MotionField:
    """
    Estimate a motion field from ordered frames and real timestamps.

    Uses intensity-weighted phase correlation between consecutive pairs and
    averages displacement rates (px/hour). Large gaps are flagged rather than
    silently interpolated.
    """
    if len(frames) != len(timestamps):
        raise ValueError("frames and timestamps must have the same length")
    if len(frames) < 2:
        raise ValueError("estimate_motion requires at least two frames")

    times = [_parse_time(t) for t in timestamps]
    order = sorted(range(len(times)), key=lambda i: times[i])
    times = [times[i] for i in order]
    frames = [frames[i] for i in order]

    decoded: list[np.ndarray] = []
    hashes: list[str] = []
    for frame in frames:
        bins, h = _to_bins(frame)
        if min_bin > 0:
            bins = bins.copy()
            bins[bins < min_bin] = NODATA
        decoded.append(bins)
        hashes.append(h)

    shape = decoded[0].shape
    for b in decoded[1:]:
        if b.shape != shape:
            raise ValueError("All frames must share the same shape")

    u_acc = np.zeros(shape, dtype=np.float64)
    v_acc = np.zeros(shape, dtype=np.float64)
    w_acc = np.zeros(shape, dtype=np.float64)
    gap_flags: list[str] = []
    pair_count = 0
    mean_us: list[float] = []
    mean_vs: list[float] = []

    for i in range(len(decoded) - 1):
        dt_min = (times[i + 1] - times[i]).total_seconds() / 60.0
        if dt_min <= 0:
            gap_flags.append(f"non_positive_dt_between_{i}_and_{i+1}")
            continue
        if dt_min > max_gap_minutes:
            gap_flags.append(
                f"large_gap_{dt_min:.1f}min_between_{times[i].isoformat()}_and_{times[i+1].isoformat()}"
            )
            continue

        a = _intensity(decoded[i])
        b = _intensity(decoded[i + 1])
        # Light blur stabilizes phase correlation on sparse fields.
        a_s = ndimage.gaussian_filter(a, sigma=1.0)
        b_s = ndimage.gaussian_filter(b, sigma=1.0)
        dy, dx, peak = _phase_correlation_shift(a_s, b_s)
        dt_h = dt_min / 60.0
        u_rate = dx / dt_h
        v_rate = dy / dt_h
        weight = max(peak, 1e-6)
        # Uniform field for this baseline estimator (global advection).
        u_acc += u_rate * weight
        v_acc += v_rate * weight
        w_acc += weight
        mean_us.append(u_rate)
        mean_vs.append(v_rate)
        pair_count += 1

    if pair_count == 0:
        # No usable pairs — zero motion with low confidence, gaps surfaced.
        u = np.zeros(shape, dtype=np.float32)
        v = np.zeros(shape, dtype=np.float32)
        conf = np.zeros(shape, dtype=np.float32)
        mean_u = mean_v = 0.0
    else:
        u = (u_acc / np.maximum(w_acc, 1e-12)).astype(np.float32)
        v = (v_acc / np.maximum(w_acc, 1e-12)).astype(np.float32)
        conf = (w_acc / np.max(w_acc)).astype(np.float32)
        mean_u = float(np.mean(mean_us))
        mean_v = float(np.mean(mean_vs))

    prov = provenance(
        kind="motion_field",
        source_hashes=hashes,
        parameters={
            "method": "phase_correlation_global",
            "max_gap_minutes": max_gap_minutes,
            "min_bin": min_bin,
            "pair_count": pair_count,
            "timestamps": [t.isoformat().replace("+00:00", "Z") for t in times],
        },
        notes="; ".join(gap_flags) if gap_flags else None,
    )
    return MotionField(
        u_px_per_hour=u,
        v_px_per_hour=v,
        confidence=conf,
        method="phase_correlation_global",
        provenance=prov,
        mean_u_px_per_hour=mean_u,
        mean_v_px_per_hour=mean_v,
        gap_flags=tuple(gap_flags),
    )
