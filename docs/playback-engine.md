# RadarVault playback engine

`static/playback.js` is a framework-free playback controller. It owns frame
timing and decoding, while the application supplies a render callback when it
wants to put the decoded image on a Leaflet overlay or an ordinary `<img>`.
The file also exports CommonJS from Node so its behavior can be tested without
network access or a browser.

## Frozen API

```js
const controller = window.RadarVaultPlayback.create({
  render(decoded, frame, index, details) {
    // `decoded.element` is an Image in the browser default decoder.
    overlay.setUrl(decoded.url);
  },
  speed: 6,
  timeMode: "uniform",
});

await controller.load([
  {
    filename: "frame-001.png",
    utc: "2026-07-11T20:00:00Z",
    observed_at: "2026-07-11T20:00:00Z",
    preview_url: "/api/cache/KXXX/preview/frame-001.webp",
    url: "/api/cache/KXXX/frame/frame-001.png",
  },
]);
controller.play();
controller.pause();
await controller.seek(12);
controller.setSpeed(10);
controller.setTimeMode("observed");
console.log(controller.getState());
controller.destroy();
```

`load`, `seek`, and `play` return promises where a decode must be awaited; the
other mutators return the controller. `getState()` always reports `index`,
`frame_count`, `playing`, `speed`, `time_mode`, and `last_error`. It also
reports cache/decode counts, warnings, the active frame, and reduced-motion
status for diagnostics.

Frame records prefer `preview_url` and fall back to `url` if the preview fails.
Applications that need a custom decoder can provide `decodeFrame(frame,
{url, signal})`; the callback is attempted once per candidate URL. An
`AbortSignal` is supplied so stale seeks and reloads can cancel network or
bitmap work.

## Bounded work and stale cancellation

The controller never starts more than four decodes at once and keeps at most
four decoded records in its LRU cache. `load`, `seek`, `pause`, and `destroy`
cancel obsolete animation callbacks. A generation token prevents a decode
started for an older seek from becoming visible after a newer seek completes.
The engine prefetches only the current frame and a small neighborhood; it does
not fetch or decode an entire archive list.

The render callback is invoked only after the decoder resolves. If a callback
throws, the error is placed in `last_error` and reported through `onError`
without corrupting the scheduler.

## Timing modes

`uniform` gives every frame the same duration (`1000 / speed` milliseconds).
`observed` uses `observed_at`, falling back to `utc`, to derive the duration
between adjacent frames. For observed playback, `speed = 6` is the baseline
real-time scaling; increasing speed shortens each source-time interval. Missing
or invalid timestamps fall back to uniform timing.

An acquisition gap cannot freeze the UI indefinitely. Gaps are capped at
`maxGapMs` (10 seconds by default), and `getState().last_warning` reports the
cap, for example `Observed gap of 180s capped at 10s`. Applications can choose
a different cap when constructing the controller.

One-frame sequences render once and never enter a false playing state. Playback
loops from the final frame to the first frame for multi-frame sequences.

## Reduced motion and integration guidance

The engine reads `prefers-reduced-motion` unless `reducedMotion` is supplied in
options. Render callbacks receive `{reducedMotion, crossfade}`; optional image
crossfades should be disabled when `crossfade` is false. `static/playback.css`
contains a small optional shell and a reduced-motion media rule, but does not
assume any application markup.

WT7 should load this file before its application wiring and delegate overlay
play/pause/seek to the controller. The current `static/app.js` remains
unchanged in WT2 so the lane can be merged without frontend integration
conflicts.
