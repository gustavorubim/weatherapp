# Analysis methods (WT6)

**Status:** experimental reflectivity-only analysis. Not a severe-weather forecast product.

RadarVault analysis operates on cached WMS PNG/RGBA frames. Raw archives are never
modified. Every derived artifact records source content hashes and processing
parameters.

## Reflectivity bins

`decode_reflectivity_bins` maps image colors to documented ordinal bins using the
`nws_like_bref_v1` palette (approximate NWS-like base-reflectivity colors).

| Code | Meaning |
|---|---|
| `>= 0` | Ordinal reflectivity bin (see palette table in code) |
| `-1` (`NODATA`) | Transparent / missing echo |
| `-2` (`UNKNOWN`) | Opaque color outside the documented palette |

Approximate dBZ midpoints are reported for human context only. WMS color tables are
not calibrated Level-II moment data.

## Clutter

`build_clutter_frequency(frames, min_presence=0.8)` returns:

- a boolean **mask** of pixels present in at least `min_presence` of frames
- a frequency field
- a metrics report and provenance

Output is mask + metrics only. Sources are never rewritten.

## Cell detection and tracking

`detect_cells` labels connected regions at or above `min_bin` and returns area,
centroid, bounding box, maximum bin, and mean bin.

`track_cells` associates cells across timestamps with a maximum-speed gate
(`max_speed_kmh` with configurable `km_per_pixel`). Births, deaths, merges, and
splits are recorded in track notes without raising.

## Motion and nowcast

`estimate_motion` uses ordered frames and **real timestamps**. Large gaps are
flagged in `gap_flags` rather than silently interpolated.

`advect_nowcast` supports lead times **5, 15, 30, and 60** minutes.
`persistence_nowcast` is the mandatory baseline (future equals last observation).

Outputs are labeled experimental and do not claim severe-weather prediction.

## Evaluation

`evaluate_nowcast` reports CSI/IoU, precision, recall, and centroid displacement
error at documented bin thresholds.

`split_by_time_blocks` splits by complete time blocks / chronological halves —
never by adjacent random frames — and **refuses overlapping source hashes** across
compared splits.

## Why reflectivity-only imagery cannot support rotation or tornado inference

Base reflectivity is a scalar intensity field (returned power). Tornadic rotation
and mesocyclones are diagnosed from **Doppler radial velocity** (and preferably
dual-polarization products), which encode motion toward/away from the radar and
hydrometeor type. A single-channel reflectivity image:

- contains no radial-velocity couplet information
- cannot distinguish rotation from translation or expansion of echo
- cannot identify debris signatures that rely on correlation coefficient / ZDR

Therefore RadarVault analysis must not claim tornado, mesocyclone, or severe-wind
prediction from reflectivity frames alone. Advection nowcasts are short-horizon
echo-motion extrapolations only.

## CLI

```bash
pip install -r requirements-analysis.txt
python -m app.analysis_cli cells KTBW --start ... --end ... --dry-run
python -m app.analysis_cli nowcast KTBW --lead-minutes 15 --dry-run
python -m app.analysis_cli evaluate --fixture synthetic-moving-cell
```

Optional `--cache-dir`, `--output`, and `--overlay-dir` accept explicit paths.
Environment-variable wiring is reserved for WT7.
