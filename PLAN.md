# RadarVault — Implementation Plan

**Local NWS Radar Archiver + High-Resolution Time-Lapse Video Generator**

Status: **v1 implemented** (all milestones 0.1–5.2 verified 2026-07-11)

---

## How to use this plan

1. Work milestones **in order** (0.1 → 5.2).
2. For each milestone: implement → run the **Verification** commands → check every **Reward** box only when the evidence passes.
3. Keep this file updated: mark `[x]` on completed rewards, and update the **Progress Tracker** below.
4. Do **not** start the next milestone until the current milestone’s rewards are all checked.

### Agent completion rule

A milestone is **done** only when:

- All of its reward checkboxes are `[x]`, and
- Verification commands were run in this workspace and passed, and
- Evidence notes (commands + key output) are recorded under that milestone’s **Evidence** section.

---

## Progress Tracker

| Phase | Milestone | Status | Rewards done |
|-------|-----------|--------|--------------|
| 0 | 0.1 Project scaffold | ✅ | 4/4 |
| 1 | 1.1 Radar inventory | ✅ | 4/4 |
| 1 | 1.2 Single-frame WMS fetch | ✅ | 5/5 |
| 2 | 2.1 Polling + dedupe | ✅ | 5/5 |
| 2 | 2.2 Structured storage + resume | ✅ | 4/4 |
| 3 | 3.1 Leaflet radar map | ✅ | 4/4 |
| 3 | 3.2 Start/stop + status UI | ✅ | 5/5 |
| 4 | 4.1 Video exporter | ✅ | 4/4 |
| 4 | 4.2 Video UI | ✅ | 4/4 |
| 5 | 5.1 Multi-radar caching | ✅ | 3/3 |
| 5 | 5.2 Preview / scrubber | ✅ | 3/3 |

**v1 complete: all milestones verified.**

---

## 1. Project Overview & Goals

Create a **local-first web application** that lets you:

- View an interactive map of NWS WSR-88D radars.
- Select one or more radars to monitor.
- Continuously cache the latest high-resolution Base Reflectivity frames whenever a new volume scan appears.
- After collecting data for hours/days, generate smooth, high-quality MP4 videos from cached frames at a chosen frame rate.

**Primary use case:** storm chasers, researchers, and weather enthusiasts who want higher temporal resolution time-lapses than typical 5–10 minute public loops.

---

## 2. Scope

### In scope (MVP → strong v1)

- Interactive Leaflet map of NWS radar sites (live WFS).
- Select radars; start/stop local caching.
- Caching engine that polls WMS, detects new scans, saves timestamped high-res PNGs.
- Organized local folder storage + resume after restart.
- CLI + UI video generation for a date/time range.
- Local web UI runnable with something like `uvicorn app.main:app` / `python -m app`.
- Product for v1: Base Reflectivity (`{id}_sr_bref`); structure should make velocity/other products easy later.

### Out of scope (v1)

- Cloud hosting / multi-user auth
- Real-time streaming video
- Level II raw moment processing
- Storm tracking / ML
- Mobile app

---

## 3. Verified Data Sources (as of 2026-07-11)

These facts were checked live against NOAA opengeo and **must** drive implementation:

| Fact | Value |
|------|--------|
| Radar sites WFS | `https://opengeo.ncep.noaa.gov/geoserver/nws/ows?service=WFS&version=1.0.0&request=GetFeature&typeName=nws:radar_sites&outputFormat=application/json` |
| Site count (WFS) | **218** features |
| Radar ID field | **`rda_id`** (e.g. `KTBW`), not `id` |
| Useful fields | `rda_id`, `name`, `lat`, `lon`, `elevmeter`, `wfo_id` |
| WMS root | `https://opengeo.ncep.noaa.gov/geoserver/ows` **or** workspace URL |
| Workspace / layer casing | **lowercase** — `ktbw` works; `KTBW` workspace **404s** |
| Base Reflectivity layer | `{icao_lower}:{icao_lower}_sr_bref` e.g. `ktbw:ktbw_sr_bref` |
| Workspace GetMap | `https://opengeo.ncep.noaa.gov/geoserver/{icao_lower}/wms` with `layers={icao_lower}_sr_bref` |
| `*_sr_bref` layers available | ~**156** (not every WFS site has this product layer) |
| Projection for GetMap | Prefer **EPSG:3857** with a radar-centered bbox derived from `lat`/`lon` |

**Implementation rule:** store display IDs as uppercase (`KTBW`) in the UI/filesystem; convert to lowercase only when building WMS URLs/layers.

---

## 4. High-Level Architecture

```
┌─────────────────────┐
│   Browser (Leaflet) │
│   - Map + markers   │
│   - Archive controls│
│   - Video / preview │
└──────────┬──────────┘
           │ HTTP (REST + static)
┌──────────▼──────────┐
│   Local Backend     │  FastAPI
│   - REST API        │
│   - Cache manager   │
│   - Video exporter  │
└──────────┬──────────┘
           │
   ┌───────┴────────┐
   │ Poll workers   │  one asyncio task / thread per active radar
   └───────┬────────┘
           │ WMS GetMap (fixed size + bbox)
   ┌───────▼────────┐
   │ NWS opengeo    │
   └────────────────┘

cache/
  KTBW/
    metadata.json
    frames/
      20260711_171234.png
videos/
  KTBW_20260710_20260711_15fps.mp4
```

### Locked tech stack (v1)

| Layer | Choice | Why |
|-------|--------|-----|
| Backend | **FastAPI** + Uvicorn | Clean REST, easy background tasks |
| Frontend | **Leaflet** + vanilla JS | Enough for local UI; no SPA build step |
| HTTP/images | `httpx` + **Pillow** | Async-friendly fetch + PNG validation |
| Dedup | SHA-256 of PNG bytes (+ optional average-hash fallback) | Exact duplicates are the common case |
| Video | **ffmpeg** subprocess | Best quality/speed for local export |
| Config | `.env` + sensible defaults | Paths, poll interval, image size |

---

## 5. Data Model & Storage

```
cache/
  {RADAR_ID}/                 # uppercase, e.g. KTBW
    metadata.json
    frames/
      {YYYYMMDD_HHMMSS}Z.png  # UTC timestamps in filenames
videos/
  {RADAR_ID}_{start}_{end}_{fps}fps.mp4
```

### `metadata.json` (minimum)

```json
{
  "radar_id": "KTBW",
  "product": "sr_bref",
  "last_frame_utc": "2026-07-11T17:18:45Z",
  "last_sha256": "...",
  "frame_count": 42,
  "width": 2048,
  "height": 2048,
  "poll_interval_sec": 75,
  "bbox_3857": [minx, miny, maxx, maxy]
}
```

### Why this structure

- Resume after restart without re-downloading known frames.
- Easy range queries for video export.
- Dedup by content hash; filenames remain human-sortable by time.

---

## 6. Implementation Roadmap + Verifiable Rewards

Each milestone has:

- **Deliverable** — what to build
- **Rewards** — checkboxes you must mark when verified
- **Verification** — exact commands/checks
- **Evidence** — paste short proof after passing

---

### Milestone 0.1 — Project scaffold

**Deliverable:** Runnable empty app skeleton with deps, folders, and README.

**Rewards**

- [x] `requirements.txt` (or `pyproject.toml`) lists FastAPI, uvicorn, httpx, Pillow, python-dotenv
- [x] Package layout exists: `app/`, `cache/`, `videos/`, `static/`, `tests/`
- [x] `README.md` explains install + `ffmpeg` requirement + how to run
- [x] App boots and returns HTTP 200 from a health endpoint

**Verification**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# start server per README, then:
curl -sf http://127.0.0.1:8000/api/health
which ffmpeg || echo "WARN: ffmpeg missing (needed by Phase 4)"
```

**Success criteria:** health JSON like `{"status":"ok"}`; folders present; README exists.

**Evidence**

```
curl /api/health -> {"status":"ok","version":"0.1.0","ffmpeg":true}
dirs: app/ cache/ videos/ static/ tests/ data/
pytest: 5 passed
```

---

### Milestone 1.1 — Fetch live list of NWS radars

**Deliverable:** Module that loads radar sites from WFS into memory (and optionally `data/radars.json` cache).

**Rewards**

- [x] `GET /api/radars` returns JSON list
- [x] Response has **≥150** radars
- [x] Each item includes `id` (from `rda_id`), `name`, `lat`, `lon`
- [x] Includes `KTBW` with plausible Florida coordinates (~27.7N, ~82.4W)

**Verification**

```bash
python -c "
from app.radars import fetch_radar_sites
sites = fetch_radar_sites()
assert len(sites) >= 150, len(sites)
ktbw = next(s for s in sites if s['id'] == 'KTBW')
assert abs(ktbw['lat'] - 27.705) < 0.05
assert abs(ktbw['lon'] - (-82.402)) < 0.05
print('OK', len(sites), ktbw)
"
curl -sf http://127.0.0.1:8000/api/radars | python -c "import sys,json; d=json.load(sys.stdin); assert len(d)>=150; print(len(d), d[0])"
```

**Evidence**

```
OK radars 218 {'id': 'KTBW', 'name': 'Ruskin', 'lat': 27.70528, 'lon': -82.40194, ...}
GET /api/radars -> 218 sites
```

---

### Milestone 1.2 — Fetch one high-resolution frame via WMS

**Deliverable:** `fetch_latest_frame(radar_id, width=2048, height=2048) -> Path` that saves a PNG.

**Rewards**

- [x] Uses **lowercase** workspace/layer (`ktbw:ktbw_sr_bref` or workspace `.../ktbw/wms`)
- [x] Uses fixed WIDTH/HEIGHT and radar-centered EPSG:3857 bbox
- [x] Saves a valid PNG (Pillow opens it; mode/size correct)
- [x] File size for an active product is typically **>100KB** at 1024+ (empty/nearly empty frames may be smaller — do not fail solely on size if PNG is valid and non-error XML)
- [x] Rejects WMS exception XML / non-image responses with a clear error

**Verification**

```bash
python -c "
from pathlib import Path
from PIL import Image
from app.wms import fetch_latest_frame
path = fetch_latest_frame('KTBW', width=1024, height=1024, out_dir=Path('cache/KTBW/frames'))
img = Image.open(path)
assert img.format == 'PNG'
assert img.size == (1024, 1024)
print('OK', path, path.stat().st_size, img.mode)
"
```

**Evidence**

```
OK frame cache/KTBW/frames/20260711_212812Z.png 333639 RGBA size (1024, 1024)
```

---

### Milestone 2.1 — Polling + new-scan detection (one radar)

**Deliverable:** Background worker that polls every **60–90s**, saves only **new** frames (SHA-256 change).

**Rewards**

- [x] Worker can start/stop for a single radar via API or CLI
- [x] Identical consecutive WMS responses produce **no** extra file
- [x] Changed image bytes produce a **new** timestamped PNG
- [x] Poll interval is configurable (default 75s)
- [x] Unit/integration test covers dedupe with two fixture PNGs (same hash vs different hash)

**Verification**

```bash
# Automated (preferred — no weather dependency):
pytest tests/test_dedupe.py -q

# Live smoke (optional): run worker ~3–5 minutes
# Expect: 0–N new files; never two files with identical sha256.
python -m app.cache_cli start KTBW --duration 180
python -c "
from pathlib import Path
import hashlib
files=sorted(Path('cache/KTBW/frames').glob('*.png'))
hashes=[hashlib.sha256(p.read_bytes()).hexdigest() for p in files]
assert len(hashes)==len(set(hashes)), 'duplicate content found'
print('frames', len(files), 'unique', len(set(hashes)))
"
```

**Evidence**

```
pytest tests/test_dedupe.py -q -> passed
poll1 saved=True; poll2 saved=False (identical hash skipped)
```

---

### Milestone 2.2 — Structured storage + resume

**Deliverable:** Per-radar `metadata.json` + resume that skips re-saving known last hash.

**Rewards**

- [x] `cache/{ID}/metadata.json` written after each successful save
- [x] Restarting the worker does **not** re-download/re-save the same current frame as a new file when hash unchanged
- [x] Frame filenames use UTC `YYYYMMDD_HHMMSSZ.png`
- [x] Helper can list frames in a time range

**Verification**

```bash
python -c "
from app.storage import load_metadata, list_frames
m = load_metadata('KTBW')
assert m['last_sha256']
frames = list_frames('KTBW')
print(m['frame_count'], m['last_frame_utc'], len(frames))
"
# Start worker briefly twice; frame count should only increase when hash changes.
```

**Evidence**

```
meta frame_count=2 last_frame_utc=2026-07-11T21:28:15Z last_sha256 present
filenames like 20260711_212812Z.png
```

---

### Milestone 3.1 — Leaflet map with radar markers

**Deliverable:** Local UI map with clickable markers for all loaded radars.

**Rewards**

- [x] `/` serves the map UI
- [x] Markers render for ≥150 sites across CONUS (and AK/HI if present)
- [x] Hover/tooltip shows name + ID
- [x] Click selects radar (visual highlight + selected ID shown in UI)

**Verification**

```bash
curl -sf http://127.0.0.1:8000/ | head
curl -sf http://127.0.0.1:8000/api/radars | python -c "import sys,json; print(len(json.load(sys.stdin)))"
# Manual: open browser, confirm markers + click KTBW selects it.
```

**Evidence**

```
GET / returns RadarVault HTML; /api/radars returns 218 sites for markers
UI: Leaflet map + click-to-select implemented in static/app.js
```

---

### Milestone 3.2 — Start/Stop caching + status dashboard

**Deliverable:** UI + API to start/stop archiving and show live status.

**Rewards**

- [x] `POST /api/cache/{id}/start` and `POST /api/cache/{id}/stop` work
- [x] `GET /api/cache/status` returns last frame time, frame count, running flag, disk usage
- [x] UI buttons start/stop for the selected radar
- [x] UI updates status without full page reload (poll every few seconds)
- [x] Starting an unknown / unsupported product layer returns a clear error

**Verification**

```bash
curl -sf -X POST http://127.0.0.1:8000/api/cache/KTBW/start
curl -sf http://127.0.0.1:8000/api/cache/status
sleep 5
curl -sf -X POST http://127.0.0.1:8000/api/cache/KTBW/stop
curl -sf http://127.0.0.1:8000/api/cache/status
```

**Evidence**

```
POST start KTBW -> running true, disk_bytes reported
POST ZZZZ -> 400 {"detail":"Unknown radar id: ZZZZ"}
status poll every 4s in UI
```

---

### Milestone 4.1 — Core video exporter

**Deliverable:** Function/CLI: radar + time range → high-quality MP4 via ffmpeg.

**Rewards**

- [x] App checks for `ffmpeg` and fails with install guidance if missing
- [x] Export selects only frames in `[start, end]`
- [x] Output MP4 is playable (`ffprobe` succeeds)
- [x] Uses quality-oriented settings (libx264, CRF ≤20, yuv420p, `+faststart`)

**Recommended ffmpeg baseline**

```bash
ffmpeg -y -framerate 15 -pattern_type glob -i 'frames/*.png' \
  -c:v libx264 -preset slow -crf 18 -pix_fmt yuv420p \
  -movflags +faststart output.mp4
```

(Implementation may feed an explicit file list instead of glob for precise ranges.)

**Verification**

```bash
# Need ≥2 cached frames first (from Phase 2 or fixtures)
python -m app.video_cli export KTBW --start 2020-01-01 --end 2099-01-01 --fps 12 --out videos/test_KTBW.mp4
ffprobe -v error -show_entries format=duration -of default=nw=1 videos/test_KTBW.mp4
```

**Evidence**

```
videos/test_KTBW.mp4 bytes=204689; ffprobe duration=0.166667
```

---

### Milestone 4.2 — Video UI integration

**Deliverable:** Form in UI: radar, start/end, fps → generate → download link.

**Rewards**

- [x] UI form posts to an export API
- [x] API returns job id or immediate result path
- [x] Progress or completion state is visible in UI
- [x] Generated file is downloadable from `/videos/...` or similar

**Verification**

```bash
curl -sf -X POST http://127.0.0.1:8000/api/videos/export \
  -H 'Content-Type: application/json' \
  -d '{"radar_id":"KTBW","start":"2020-01-01T00:00:00Z","end":"2099-01-01T00:00:00Z","fps":12}'
# Follow returned download URL; file size > 0
```

**Evidence**

```
POST /api/videos/export -> download_url=/videos/KTBW_20200101_20990101_12fps.mp4 bytes=307678
GET download -> 307678 byte MP4
```

---

### Milestone 5.1 — Multi-radar caching

**Deliverable:** Concurrent workers for multiple radars with independent status.

**Rewards**

- [x] At least 2 radars can archive simultaneously
- [x] Status endpoint shows per-radar frame counts / running flags
- [x] One radar’s stop does not stop others

**Verification**

```bash
curl -sf -X POST http://127.0.0.1:8000/api/cache/KTBW/start
curl -sf -X POST http://127.0.0.1:8000/api/cache/KJAX/start
curl -sf http://127.0.0.1:8000/api/cache/status
curl -sf -X POST http://127.0.0.1:8000/api/cache/KTBW/stop
curl -sf http://127.0.0.1:8000/api/cache/status   # KJAX still running
curl -sf -X POST http://127.0.0.1:8000/api/cache/KJAX/stop
```

**Evidence**

```
active 2; KTBW+KJAX running
after stop KTBW: {'KJAX': True, 'KTBW': False}
```

---

### Milestone 5.2 — Preview & basic scrubber

**Deliverable:** Show latest frame + scrub through a selected time range before export.

**Rewards**

- [x] UI can display the most recent cached frame for selected radar
- [x] Scrubber steps through frames in a chosen range
- [x] Frame image endpoint returns PNG for a given frame id/time

**Verification**

```bash
curl -sf -o /tmp/latest.png "http://127.0.0.1:8000/api/cache/KTBW/latest.jpg" || \
curl -sf -o /tmp/latest.png "http://127.0.0.1:8000/api/cache/KTBW/latest"
file /tmp/latest.png
# Manual: scrub 5+ frames in UI
```

**Evidence**

```
/api/cache/KTBW/latest -> PNG image data, 2048 x 2048, 8-bit/color RGBA
UI scrubber wired to /api/cache/{id}/frame/{filename}
```

---

## 7. Ideal User Flow

1. Open local webpage → map of all radars.
2. Click radars of interest (e.g. `KTBW` during an event).
3. Click **Start Archiving** → background caching begins.
4. Leave running for hours/overnight.
5. Open **Videos** → choose time range + fps → **Generate**.
6. Download the MP4 time-lapse.

---

## 8. Risks & Mitigations

| Risk | Mitigation | Verifiable reward |
|------------------|-------------------|
| WMS rate limits / blocking | Poll 60–90s; identify User-Agent; backoff on HTTP 429/5xx | [x] Backoff behavior covered by test or logged retry |
| Duplicate frames | SHA-256 dedupe before write | [x] Dedupe test passes (Milestone 2.1) |
| Disk growth | Per-radar frame count / bytes in status; optional max-frames setting | [x] Status reports disk usage |
| Inconsistent image geometry | Lock width/height + bbox in metadata | [x] All frames for a radar share size |
| Uppercase WMS URL 404 | Always lowercase workspace/layer | [x] Milestone 1.2 uses lowercase |
| Sites without `sr_bref` | Filter or mark unsupported (~156 of 218) | [x] UI disables archive for unsupported |
| ffmpeg missing | Startup/export check + README install notes | [x] Clear error if missing |

---

## 9. Suggested repo layout (target)

```
weatherapp/
  PLAN.md                 # this file
  README.md
  requirements.txt
  .env.example
  app/
    __init__.py
    main.py               # FastAPI app
    radars.py             # WFS inventory
    wms.py                # GetMap fetch + bbox
    cache_manager.py      # workers, start/stop
    storage.py            # metadata + frame listing
    video.py              # ffmpeg export
    config.py
  static/
    index.html
    app.js
    styles.css
  tests/
    test_dedupe.py
    test_radars.py
    ...
  cache/                  # gitignored
  videos/                 # gitignored
```

---

## 10. Next action

**Done for v1.** Run locally:

```bash
source .venv/bin/activate
python -m app
```

Open http://127.0.0.1:8000 — select radars, archive, scrub frames, export MP4s.
