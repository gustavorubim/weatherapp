# RadarVault

Local-first NWS radar archiver and high-resolution time-lapse video generator.

## Features

- Interactive Leaflet map of NWS WSR-88D sites
- Continuous caching of Base Reflectivity (`*_sr_bref`) via NOAA opengeo WMS
- SHA-256 deduplication so unchanged volume scans are not rewritten
- MP4 export with ffmpeg (libx264, CRF 18, yuv420p)

## Requirements

- Python 3.9+
- [ffmpeg](https://ffmpeg.org/) (required for video export)

```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt install ffmpeg
```

## Setup

```bash
cd weatherapp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # optional
```

## Run

```bash
python -m app
# or:
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

## CLI

```bash
# Poll once / start caching
python -m app.cache_cli start KTBW --once
python -m app.cache_cli start KTBW --duration 180
python -m app.cache_cli status

# Export a video from cached frames
python -m app.video_cli export KTBW --start 2020-01-01 --end 2099-01-01 --fps 12 --out videos/test_KTBW.mp4
```

## Storage layout

```
cache/
  KTBW/
    metadata.json
    frames/
      20260711_171234Z.png
videos/
  KTBW_20260710_20260711_15fps.mp4
```

## API (selected)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check |
| GET | `/api/radars` | Radar inventory |
| POST | `/api/cache/{id}/start` | Start archiving |
| POST | `/api/cache/{id}/stop` | Stop archiving |
| GET | `/api/cache/status` | Per-radar status |
| GET | `/api/cache/{id}/latest` | Latest PNG |
| GET | `/api/cache/{id}/frames` | Frame list |
| POST | `/api/videos/export` | Build MP4 |

See `PLAN.md` for milestones and verification checklist.
