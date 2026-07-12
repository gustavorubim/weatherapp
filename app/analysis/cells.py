"""Storm-cell detection and multi-frame tracking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

import numpy as np
from scipy import ndimage

from app.analysis.provenance import provenance, sha256_array
from app.analysis.reflectivity import NODATA, UNKNOWN, decode_reflectivity_bins


@dataclass(frozen=True)
class Cell:
    id: int
    area_pixels: int
    centroid_yx: tuple[float, float]
    bbox_yx: tuple[int, int, int, int]  # y0, x0, y1, x1 (exclusive)
    max_bin: int
    mean_bin: float
    source_hash: str | None = None


@dataclass(frozen=True)
class TrackPoint:
    time: datetime
    cell: Cell


@dataclass(frozen=True)
class Track:
    track_id: int
    points: tuple[TrackPoint, ...]
    status: str  # active | ended | merged | split
    speed_kmh: float | None = None
    direction_deg: float | None = None  # meteorological: degrees from north, clockwise
    notes: tuple[str, ...] = ()


def _as_bins(frame: Any) -> tuple[np.ndarray, str | None]:
    if isinstance(frame, dict) and "bins" in frame:
        bins = np.asarray(frame["bins"], dtype=np.int16)
        return bins, frame.get("source_hash") or sha256_array(bins)
    if isinstance(frame, np.ndarray) and frame.ndim == 2:
        return frame.astype(np.int16), sha256_array(frame)
    bins = decode_reflectivity_bins(frame)
    assert isinstance(bins, np.ndarray)
    return bins, sha256_array(bins)


def detect_cells(
    frame: Any,
    *,
    min_bin: int = 4,
    min_pixels: int = 20,
) -> list[Cell]:
    """
    Detect connected reflectivity regions at or above min_bin.

    Returns area, centroid, bounding box, maximum bin, and mean bin per cell.
    """
    bins, source_hash = _as_bins(frame)
    valid = (bins >= min_bin) & (bins != UNKNOWN) & (bins != NODATA)
    if not np.any(valid):
        return []

    labeled, n = ndimage.label(valid)
    cells: list[Cell] = []
    for idx in range(1, n + 1):
        mask = labeled == idx
        area = int(np.sum(mask))
        if area < min_pixels:
            continue
        ys, xs = np.nonzero(mask)
        y0, y1 = int(ys.min()), int(ys.max()) + 1
        x0, x1 = int(xs.min()), int(xs.max()) + 1
        values = bins[mask].astype(np.float64)
        cells.append(
            Cell(
                id=idx,
                area_pixels=area,
                centroid_yx=(float(ys.mean()), float(xs.mean())),
                bbox_yx=(y0, x0, y1, x1),
                max_bin=int(values.max()),
                mean_bin=float(values.mean()),
                source_hash=source_hash,
            )
        )
    cells.sort(key=lambda c: c.area_pixels, reverse=True)
    return cells


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


def _bearing_deg(dy: float, dx: float) -> float:
    # Image y increases downward; meteorological direction is from north clockwise.
    # Convert image displacement (dy down, dx right) to map (north up): north = -dy.
    angle = (np.degrees(np.arctan2(dx, -dy)) + 360.0) % 360.0
    return float(angle)


def track_cells(
    timestamped_cells: Sequence[tuple[Any, Sequence[Cell]]],
    *,
    max_speed_kmh: float = 120.0,
    km_per_pixel: float = 0.225,
) -> list[Track]:
    """
    Associate cells across time using a maximum-speed gate.

    Handles births, deaths, merges (multiple → one), and splits (one → multiple)
    without raising. Associations are greedy nearest-neighbor under the speed gate.
    """
    if not timestamped_cells:
        return []

    frames: list[tuple[datetime, list[Cell]]] = []
    for ts, cells in timestamped_cells:
        frames.append((_parse_time(ts), list(cells)))
    frames.sort(key=lambda item: item[0])

    next_track_id = 1
    active: dict[int, list[TrackPoint]] = {}
    finished: list[Track] = []
    notes_by_id: dict[int, list[str]] = {}

    def close_track(tid: int, status: str, extra: str | None = None) -> None:
        points = active.pop(tid)
        notes = list(notes_by_id.pop(tid, []))
        if extra:
            notes.append(extra)
        speed, direction = _track_motion(points, km_per_pixel)
        finished.append(
            Track(
                track_id=tid,
                points=tuple(points),
                status=status,
                speed_kmh=speed,
                direction_deg=direction,
                notes=tuple(notes),
            )
        )

    # Seed with first frame.
    t0, cells0 = frames[0]
    for cell in cells0:
        active[next_track_id] = [TrackPoint(time=t0, cell=cell)]
        notes_by_id[next_track_id] = ["birth"]
        next_track_id += 1

    for i in range(1, len(frames)):
        t_prev, _ = frames[i - 1]
        t_cur, cells_cur = frames[i]
        dt_h = max((t_cur - t_prev).total_seconds() / 3600.0, 1e-9)
        max_px = (max_speed_kmh * dt_h) / max(km_per_pixel, 1e-9)

        # Previous centroids by track id.
        prev_ids = list(active.keys())
        prev_centroids = {
            tid: active[tid][-1].cell.centroid_yx for tid in prev_ids
        }

        # Greedy matching: build candidate pairs under speed gate.
        candidates: list[tuple[float, int, int]] = []  # dist, track_id, cell_index
        for tid, (py, px) in prev_centroids.items():
            for j, cell in enumerate(cells_cur):
                cy, cx = cell.centroid_yx
                dist = float(np.hypot(cy - py, cx - px))
                if dist <= max_px:
                    candidates.append((dist, tid, j))
        candidates.sort(key=lambda x: x[0])

        matched_tracks: set[int] = set()
        matched_cells: set[int] = set()
        assignments: list[tuple[int, int]] = []  # track_id, cell_index

        for dist, tid, j in candidates:
            if tid in matched_tracks or j in matched_cells:
                # Potential merge/split conflict — record and skip secondary.
                if tid in matched_tracks and j not in matched_cells:
                    notes_by_id.setdefault(tid, []).append("split_candidate_ignored")
                if j in matched_cells and tid not in matched_tracks:
                    # Cell already taken → treat as merge candidate on the winner.
                    winner = next(t for t, cj in assignments if cj == j)
                    notes_by_id.setdefault(winner, []).append(f"merge_from_track_{tid}")
                    notes_by_id.setdefault(tid, []).append(f"merged_into_track_{winner}")
                continue
            matched_tracks.add(tid)
            matched_cells.add(j)
            assignments.append((tid, j))

        # Detect splits: one previous cell close to multiple unmatched? already gated.
        # Explicit split: if a track has multiple near-equal candidates and only one taken,
        # birth new tracks for remaining nearby cells (already births below).

        for tid, j in assignments:
            active[tid].append(TrackPoint(time=t_cur, cell=cells_cur[j]))

        # Deaths: unmatched previous tracks end.
        for tid in prev_ids:
            if tid not in matched_tracks:
                close_track(tid, "ended", "death")

        # Births: unmatched current cells.
        for j, cell in enumerate(cells_cur):
            if j in matched_cells:
                continue
            # If near an assigned cell, mark as split offspring.
            split_note = "birth"
            for tid, mj in assignments:
                py, px = cells_cur[mj].centroid_yx
                cy, cx = cell.centroid_yx
                if np.hypot(cy - py, cx - px) <= max_px * 0.75:
                    split_note = f"split_from_track_{tid}"
                    notes_by_id.setdefault(tid, []).append(f"split_to_track_{next_track_id}")
                    break
            active[next_track_id] = [TrackPoint(time=t_cur, cell=cell)]
            notes_by_id[next_track_id] = [split_note]
            next_track_id += 1

        # Soft-mark merges: multiple previous tracks claimed same cell was handled above.

    for tid in list(active.keys()):
        close_track(tid, "active")

    finished.sort(key=lambda t: t.track_id)
    return finished


def _track_motion(
    points: Sequence[TrackPoint], km_per_pixel: float
) -> tuple[float | None, float | None]:
    if len(points) < 2:
        return None, None
    y0, x0 = points[0].cell.centroid_yx
    y1, x1 = points[-1].cell.centroid_yx
    dt_h = (points[-1].time - points[0].time).total_seconds() / 3600.0
    if dt_h <= 0:
        return None, None
    dy = y1 - y0
    dx = x1 - x0
    dist_km = float(np.hypot(dy, dx) * km_per_pixel)
    speed = dist_km / dt_h
    return speed, _bearing_deg(dy, dx)


def tracks_report(tracks: Sequence[Track]) -> dict[str, Any]:
    return {
        "track_count": len(tracks),
        "tracks": [
            {
                "track_id": t.track_id,
                "status": t.status,
                "n_points": len(t.points),
                "speed_kmh": t.speed_kmh,
                "direction_deg": t.direction_deg,
                "notes": list(t.notes),
                "start": t.points[0].time.isoformat().replace("+00:00", "Z"),
                "end": t.points[-1].time.isoformat().replace("+00:00", "Z"),
                "centroids": [
                    {"t": p.time.isoformat().replace("+00:00", "Z"), "yx": list(p.cell.centroid_yx)}
                    for p in t.points
                ],
            }
            for t in tracks
        ],
        "provenance": provenance(
            kind="cell_tracks",
            source_hashes=sorted(
                {
                    p.cell.source_hash
                    for t in tracks
                    for p in t.points
                    if p.cell.source_hash
                }
            ),
            parameters={},
        ),
    }
