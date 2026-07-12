"""# RadarVault video export (WT5)

Dimension-safe, efficient H.264 export with optional background jobs.

## Goals

- Never silently pick a resolution when frames disagree.
- Avoid full temporary PNG copies (concat of originals; overhead ≪ 5%).
- Provide `archive` / `balanced` / `small` quality presets.
- Support process-local jobs with progress and cancellation for API wiring (WT7).

## Quality presets

| Preset | CRF | x264 preset | Intent |
|--------|-----|-------------|--------|
| `archive` | 15 | slow | Highest quality, larger files |
| `balanced` | 18 | medium | Default tradeoff |
| `small` | 26 | fast | Smaller / faster encodes |

## Dimension policy

| Policy | Behavior |
|--------|----------|
| `error` (default) | Inspect every frame; if multiple sizes exist, raise with a per-size inventory |
| `normalize` | Scale/pad to `target_width`×`target_height`, or the dominant size if omitted |

Mixed 1024/2048 archives must **not** silently export at 1024. Use `error` (fail clearly) or `normalize` (declare the output size).

## CLI

```bash
python -m app.video_cli export KTBW \
  --start 2020-01-01 --end 2099-01-01 \
  --quality balanced \
  --dimension-policy error \
  --out /tmp/radarvault-test.mp4

# Normalize mixed sizes to 2048x2048
python -m app.video_cli export KTBW \
  --dimension-policy normalize \
  --target-width 2048 --target-height 2048 \
  --out /tmp/radarvault-norm.mp4
```

Optional `--timestamp-overlay` burns a UTC range label (fetch-time range from selected frames).

## Python API

```python
from pathlib import Path
from app.video import export_video

path = export_video(
    "KTBW",
    start="2020-01-01",
    end="2099-01-01",
    fps=12,
    quality="balanced",
    dimension_policy="error",
    out=Path("/tmp/out.mp4"),
)
```

New optional kwargs (existing callers remain compatible):

- `quality`, `dimension_policy`, `target_width`, `target_height`
- `timestamp_overlay`
- `progress_callback(progress: float, message: str)`
- `cancel_event: threading.Event`

## Background jobs

```python
from app.video_jobs import VideoJobManager, VideoJobRequest

mgr = VideoJobManager(max_concurrent=1, retention_seconds=3600)
job = mgr.submit(VideoJobRequest(radar_id="KTBW", fps=12, quality="small"))
print(mgr.status(job.job_id).to_dict())
mgr.cancel(job.job_id)
```

States: `queued` → `running` → `complete` | `failed` | `cancelled`.

- Progress is monotonic in `[0, 1]` for successful jobs.
- Cancel terminates ffmpeg and removes incomplete `.partial` outputs.
- Identical completed requests may reuse the verified output path.
- `cleanup()` drops terminal jobs older than `retention_seconds`.

WT7 wires HTTP endpoints (`POST /api/videos/jobs`, status, cancel) — not owned by this lane.

## Efficiency

Export builds an ffmpeg concat manifest that references source frame paths directly (hardlink/symlink staging is available as a helper). Temporary overhead is the manifest text only and must stay under 5% of selected source bytes.
"""
