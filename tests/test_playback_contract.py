from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PLAYBACK = ROOT / "static" / "playback.js"


def _run_node(source: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the playback contract harness")
    result = subprocess.run(
        [node, "-e", source],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        raise AssertionError(f"Node harness failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise AssertionError(f"Node harness did not return JSON:\n{result.stdout}\n{result.stderr}") from exc


def test_playback_script_parses():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the playback contract harness")
    result = subprocess.run([node, "--check", str(PLAYBACK)], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_bounded_scheduler_modes_and_stale_seek():
    source = f"""
const assert = require('assert');
const {{ create }} = require({json.dumps(str(PLAYBACK))});

(async () => {{

let clock = 0;
let rafId = 0;
const rafs = new Map();
const requestAnimationFrame = (cb) => {{ const id = ++rafId; rafs.set(id, cb); return id; }};
const cancelAnimationFrame = (id) => rafs.delete(id);
async function step(ms) {{
  clock += ms;
  const pending = [...rafs.values()];
  rafs.clear();
  pending.forEach((cb) => cb(clock));
  await Promise.resolve();
  await Promise.resolve();
}}
const frames = Array.from({{length: 1000}}, (_, i) => ({{
  filename: `frame-${{i}}.png`,
  utc: new Date(Date.UTC(2026, 6, 11, 20, 0, i % 60)).toISOString(),
  observed_at: new Date(Date.UTC(2026, 6, 11, 20, 0, i % 60)).toISOString(),
  preview_url: `/preview/${{i}}.webp`,
  url: `/full/${{i}}.png`,
}}));
const decodeCalls = [];
let active = 0;
let maxActive = 0;
const decodeFrame = async (frame, {{url}}) => {{
  active += 1;
  maxActive = Math.max(maxActive, active);
  decodeCalls.push(url);
  await Promise.resolve();
  active -= 1;
  return {{ url, frame: frame.filename }};
}};
const rendered = [];
const controller = create({{ decodeFrame, render: (decoded, frame, index, details) => rendered.push({{ url: decoded.url, index, details }}),
  requestAnimationFrame, cancelAnimationFrame, now: () => clock, speed: 6 }});
await controller.load(frames);
assert.equal(controller.getState().frame_count, 1000);
assert(decodeCalls.length <= 4, `eager decode count ${{decodeCalls.length}}`);
assert(maxActive <= 4, `max active ${{maxActive}}`);
assert(controller.getState().pending_decodes <= 4);
assert(controller.getState().decoded_cache_size <= 4);
assert.equal(rendered.at(-1).index, 0);

await controller.play();
await step(1); // establish the rAF clock
await step(200); // uniform duration is ~167ms
assert.equal(controller.getState().index, 1);
const beforePause = controller.getState().index;
controller.pause();
await step(1000);
assert.equal(controller.getState().index, beforePause);
await controller.seek(500);
assert.equal(controller.getState().index, 500);
assert.equal(rendered.at(-1).index, 500);

controller.setTimeMode('observed');
assert.equal(controller.getState().time_mode, 'observed');
const gapFrames = [
  {{ filename: 'a', utc: '2026-07-11T20:00:00Z', observed_at: '2026-07-11T20:00:00Z', url: '/a' }},
  {{ filename: 'b', utc: '2026-07-11T20:10:00Z', observed_at: '2026-07-11T20:10:00Z', url: '/b' }},
];
await controller.load(gapFrames, {{ timeMode: 'observed' }});
const gapState = controller.getState();
assert(gapState.current_duration_ms <= 10000);
assert(gapState.last_warning.includes('capped'));

const one = create({{ decodeFrame, requestAnimationFrame, cancelAnimationFrame, now: () => clock }});
await one.load([{{ filename: 'only', url: '/only' }}]);
await one.play();
assert.equal(one.getState().playing, false);

const staleRendered = [];
const deferred = new Map();
const controlledDecode = (frame, {{url, signal}}) => new Promise((resolve, reject) => {{
  const entry = {{ resolve: () => resolve({{ url, frame: frame.filename }}), reject }};
  deferred.set(url, entry);
  if (signal) signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), {{name: 'AbortError'}})), {{ once: true }});
}});
const stale = create({{ decodeFrame: controlledDecode, render: (decoded, frame) => staleRendered.push(frame.filename),
  requestAnimationFrame, cancelAnimationFrame, now: () => clock }});
const loading = stale.load([
  {{ filename: 'old', preview_url: '/old-preview', url: '/old' }},
  {{ filename: 'new', preview_url: '/new-preview', url: '/new' }},
  {{ filename: 'newer', preview_url: '/newer-preview', url: '/newer' }},
]);
await Promise.resolve();
assert(deferred.has('/old-preview'));
const seeking = stale.seek(2);
await Promise.resolve();
assert(deferred.has('/newer-preview'));
deferred.get('/newer-preview').resolve();
await seeking;
await loading;
assert.deepEqual(staleRendered, ['newer']);

const reduced = create({{ reducedMotion: true, decodeFrame }});
assert.equal(reduced.getState().reduced_motion, true);
controller.destroy();
assert.equal(rafs.size, 0);
const destroyedState = controller.getState();
assert.equal(destroyedState.destroyed, true);

console.log(JSON.stringify({{ maxActive, eagerDecodes: decodeCalls.length, rendered: rendered.length,
  finalIndex: destroyedState.index, reducedMotion: reduced.getState().reduced_motion }}));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
    evidence = _run_node(source)
    assert evidence["maxActive"] <= 4
    assert evidence["eagerDecodes"] <= 16
    assert evidence["rendered"] >= 3
    assert evidence["reducedMotion"] is True


def test_preview_failure_falls_back_to_full_url():
    source = f"""
const assert = require('assert');
const {{ create }} = require({json.dumps(str(PLAYBACK))});
const attempts = [];
const decodeFrame = async (frame, {{url}}) => {{
  attempts.push(url);
  if (url.includes('preview')) throw new Error('preview unavailable');
  return {{ url }};
}};
(async () => {{
  const seen = [];
  const controller = create({{ decodeFrame, render: (decoded) => seen.push(decoded.url) }});
  await controller.load([{{ filename: 'x', preview_url: '/preview/x.webp', url: '/full/x.png' }}]);
  assert.deepEqual(attempts.slice(0, 2), ['/preview/x.webp', '/full/x.png']);
  assert.deepEqual(seen, ['/full/x.png']);
  console.log(JSON.stringify({{ attempts, seen }}));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
    evidence = _run_node(source)
    assert evidence["attempts"][:2] == ["/preview/x.webp", "/full/x.png"]
    assert evidence["seen"] == ["/full/x.png"]
