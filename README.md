# RadarVault

**Local NWS radar archiver + high-resolution time-lapse generator.**

Cache every new volume scan from NOAA opengeo, then scrub frames on a map or export a smooth MP4 — without uploading anything to the cloud.

<p>
  <img alt="Python" src="https://img.shields.io/badge/python-3.9%2B-3dba8a?style=flat-square" />
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-local%20web%20UI-5b9fd4?style=flat-square" />
  <img alt="ffmpeg" src="https://img.shields.io/badge/video-ffmpeg%20libx264-f0b429?style=flat-square" />
</p>

---

## Why

Public radar loops are often sparse (every few minutes). RadarVault polls WMS on a polite interval, keeps only **new** scans, and lets you build denser time-lapses for storms you care about — overnight, on your machine.

## Features

| | |
|---|---|
| **Map of NWS sites** | Leaflet UI for WSR-88D and airport TDWR radars |
| **Smart products** | Prefers `sr_bref` (WSR-88D), falls back to `bref1` / `brefl` (TDWR) |
| **Local archive** | Timestamped PNGs under `cache/{RADAR_ID}/frames/` |
| **Deduped polling** | SHA-256 skip so identical frames are not rewritten |
| **Map overlay** | Play cached frames georeferenced over the basemap |
| **MP4 export** | High-quality H.264 via ffmpeg (CRF 18, yuv420p, faststart) |
| **Multi-radar** | Archive several sites at once with independent status |

## Quick start

**1. Install dependencies**

```bash
# ffmpeg (required for video export)
brew install ffmpeg          # macOS
# sudo apt install ffmpeg    # Ubuntu/Debian

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**2. Run**

```bash
python -m app
```

Open **[http://127.0.0.1:8000](http://127.0.0.1:8000)**.

**3. Use the UI**

1. Click a radar (green = WSR-88D, blue = TDWR).
2. **Start archiving** and leave it running through the event.
3. Scrub frames in the sidebar, or **Play on map** for a georeferenced loop.
4. Pick a time range + FPS → **Generate MP4** → download.

Optional config: copy `.env.example` → `.env` (poll interval, image size, host/port).

---

## CLI

Useful for smoke tests or headless archiving:

```bash
# One poll
python -m app.cache_cli start KTBW --once
python -m app.cache_cli start TMCO --once    # TDWR (Orlando)

# Run for a while, then stop
python -m app.cache_cli start KTBW --duration 180
python -m app.cache_cli status

# Export from cached frames
python -m app.video_cli export KTBW \
  --start 2020-01-01 --end 2099-01-01 \
  --fps 12 --out videos/ktbw_loop.mp4
```

---

## Storage

```text
cache/
  KTBW/
    metadata.json          # last hash, bbox, frame count, product
    frames/
      20260711_171234Z.png
  TMCO/
    ...
videos/
  KTBW_20260710_20260711_15fps.mp4
```

Frame names are UTC (`YYYYMMDD_HHMMSSZ.png`). Restarting the app resumes from `metadata.json` and will not re-save an unchanged scan.

---

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | Liveness + ffmpeg availability |
| `GET` | `/api/radars` | Full site inventory + product support |
| `POST` | `/api/cache/{id}/start` | Start archiving |
| `POST` | `/api/cache/{id}/stop` | Stop archiving |
| `GET` | `/api/cache/status` | Per-radar status / disk usage |
| `GET` | `/api/cache/{id}/frames` | List cached frames |
| `GET` | `/api/cache/{id}/latest` | Latest PNG |
| `GET` | `/api/cache/{id}/overlay` | Frames + geographic bounds for map play |
| `POST` | `/api/videos/export` | Build MP4 → `/videos/{filename}` |

---

## Notes

- **Data source:** [NOAA opengeo](https://opengeo.ncep.noaa.gov/geoserver/) WFS (sites) + WMS (imagery).
- **Polling:** default ~75s; backs off on WMS errors. Be a good citizen — don’t hammer the service.
- **Coverage:** not every WFS site has a reflectivity layer; the UI disables archiving when none is available.
- **Plan / checklist:** see [`PLAN.md`](PLAN.md).

## License

Use freely for personal research and education. Radar imagery remains subject to NOAA / NWS terms of use.
