/*
 * RadarVault playback engine.
 *
 * This file deliberately has no dependency on the rest of the UI.  WT7 can
 * provide a render callback (or an image element) when it wires the engine to
 * Leaflet.  It is also CommonJS-compatible so the contract can be exercised
 * by a deterministic Node harness without a browser.
 */
((root, factory) => {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.RadarVaultPlayback = api;
})(typeof globalThis !== "undefined" ? globalThis : this, (root) => {
  "use strict";

  const MAX_CONCURRENCY = 4;
  const MAX_CACHE = 4;
  const DEFAULT_SPEED = 6;
  const DEFAULT_MAX_GAP_MS = 10_000;
  const DEFAULT_MIN_FRAME_MS = 50;

  function clamp(value, lower, upper) {
    return Math.min(upper, Math.max(lower, value));
  }

  function finiteNumber(value, fallback) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function frameDate(frame) {
    if (!frame || typeof frame !== "object") return null;
    const candidates = [frame.observed_at, frame.utc];
    for (const candidate of candidates) {
      if (!candidate) continue;
      const timestamp = Date.parse(candidate);
      if (Number.isFinite(timestamp)) return timestamp;
    }
    return null;
  }

  function uniqueUrls(frame) {
    if (!frame || typeof frame !== "object") return [];
    return [...new Set([frame.preview_url, frame.url].filter((url) => typeof url === "string" && url))];
  }

  function abortError() {
    const error = new Error("Playback decode cancelled");
    error.name = "AbortError";
    return error;
  }

  function throwIfAborted(signal) {
    if (signal && signal.aborted) throw abortError();
  }

  /** Load and decode one URL.  The engine handles preview-to-full fallback. */
  function defaultDecode(url, { signal } = {}) {
    if (typeof Image === "undefined") {
      return Promise.reject(new Error("Image decoding is unavailable in this environment"));
    }
    throwIfAborted(signal);
    return new Promise((resolve, reject) => {
      const image = new Image();
      let settled = false;
      const cleanup = () => {
        if (signal) signal.removeEventListener("abort", onAbort);
      };
      const finish = (callback, value) => {
        if (settled) return;
        settled = true;
        cleanup();
        callback(value);
      };
      const onAbort = () => {
        try { image.src = ""; } catch (_) { /* best effort */ }
        finish(reject, abortError());
      };
      image.decoding = "async";
      image.loading = "eager";
      image.alt = "";
      image.onload = () => finish(resolve, { element: image, url });
      image.onerror = () => finish(reject, new Error(`Unable to decode radar frame: ${url}`));
      if (signal) signal.addEventListener("abort", onAbort, { once: true });
      image.src = url;
      // decode() resolves after the image is decoded, which is stronger than
      // onload for browsers that expose it.  onload remains the fallback.
      if (typeof image.decode === "function") {
        image.decode().then(
          () => finish(resolve, { element: image, url }),
          () => { /* onerror/onload reports the final result */ },
        );
      }
    });
  }

  class PlaybackController {
    constructor(options = {}) {
      this.options = { ...options };
      this.render = typeof options.render === "function"
        ? options.render
        : (typeof options.onFrame === "function" ? options.onFrame : null);
      this.target = options.element || options.imageElement || null;
      this.decodeFrame = typeof options.decodeFrame === "function" ? options.decodeFrame : null;
      this.maxConcurrent = clamp(Math.floor(finiteNumber(options.maxConcurrent, MAX_CONCURRENCY)), 1, MAX_CONCURRENCY);
      this.maxCache = clamp(Math.floor(finiteNumber(options.maxCache, MAX_CACHE)), 1, MAX_CACHE);
      this.maxGapMs = Math.max(DEFAULT_MIN_FRAME_MS, finiteNumber(options.maxGapMs, DEFAULT_MAX_GAP_MS));
      this.minFrameMs = Math.max(1, finiteNumber(options.minFrameMs, DEFAULT_MIN_FRAME_MS));
      this.speed = clamp(finiteNumber(options.speed ?? options.fps, DEFAULT_SPEED), 0.1, 120);
      this.timeMode = options.timeMode === "observed" ? "observed" : "uniform";
      this.reducedMotion = options.reducedMotion === undefined
        ? Boolean(root && typeof root.matchMedia === "function" && root.matchMedia("(prefers-reduced-motion: reduce)").matches)
        : Boolean(options.reducedMotion);
      this.onStateChange = typeof options.onStateChange === "function" ? options.onStateChange : null;
      this.onError = typeof options.onError === "function" ? options.onError : null;
      this.raf = typeof options.requestAnimationFrame === "function"
        ? options.requestAnimationFrame
        : (root && typeof root.requestAnimationFrame === "function"
          ? root.requestAnimationFrame.bind(root)
          : (callback) => setTimeout(() => callback(this.now()), 16));
      this.cancelRaf = typeof options.cancelAnimationFrame === "function"
        ? options.cancelAnimationFrame
        : (root && typeof root.cancelAnimationFrame === "function"
          ? root.cancelAnimationFrame.bind(root)
          : (id) => clearTimeout(id));
      this.now = typeof options.now === "function"
        ? options.now
        : (() => (root && root.performance && typeof root.performance.now === "function" ? root.performance.now() : Date.now()));

      this.frames = [];
      this.index = 0;
      this.playing = false;
      this.rafId = null;
      this.lastRafTime = null;
      this.frameElapsed = 0;
      this.generation = 0;
      this.destroyed = false;
      this.lastError = null;
      this.lastWarning = null;
      this.cache = new Map();
      this.pending = new Map();
      this.queue = [];
      this.activeDecodes = 0;
      this.ownedObjectUrls = new Set();
    }

    load(frames, options = {}) {
      this._assertUsable();
      const nextFrames = Array.isArray(frames) ? frames.filter((frame) => frame && typeof frame === "object") : [];
      this._cancelScheduled();
      this._cancelPending();
      this._clearCache();
      this.frames = nextFrames.slice();
      this.generation += 1;
      const generation = this.generation;
      this.index = this.frames.length
        ? clamp(Math.floor(finiteNumber(options.index, 0)), 0, this.frames.length - 1)
        : 0;
      if (options.speed !== undefined || options.fps !== undefined) {
        this.speed = clamp(finiteNumber(options.speed ?? options.fps, this.speed), 0.1, 120);
      }
      if (options.timeMode === "uniform" || options.timeMode === "observed") this.timeMode = options.timeMode;
      this.playing = false;
      this.lastRafTime = null;
      this.frameElapsed = 0;
      this.lastError = null;
      this.lastWarning = null;
      this._emitState();
      if (!this.frames.length) return Promise.resolve(null);
      return this._ensureDecoded(this.index, generation, true)
        .then((decoded) => {
          if (generation !== this.generation || this.destroyed) return null;
          if (decoded) this._display(this.index, decoded, generation);
          this._prefetchAround(this.index, generation);
          return decoded;
        })
        .catch((error) => {
          if (generation !== this.generation || this.destroyed || error && error.name === "AbortError") return null;
          this._reportError(error);
          return null;
        });
    }

    play() {
      this._assertUsable();
      if (this.frames.length < 2) {
        this.playing = false;
        this._cancelScheduled();
        this._emitState();
        return Promise.resolve(false);
      }
      if (this.playing) return Promise.resolve(true);
      this.playing = true;
      this.lastRafTime = null;
      this.frameElapsed = 0;
      const generation = this.generation;
      this._prefetchAround(this.index, generation);
      this._schedule();
      this._emitState();
      return Promise.resolve(true);
    }

    pause() {
      this._assertUsable();
      this.playing = false;
      this.lastRafTime = null;
      this.frameElapsed = 0;
      this._cancelScheduled();
      this._emitState();
      return this;
    }

    seek(index) {
      this._assertUsable();
      this._cancelScheduled();
      this._cancelPending();
      this.generation += 1;
      const generation = this.generation;
      this.index = this.frames.length ? clamp(Math.floor(finiteNumber(index, 0)), 0, this.frames.length - 1) : 0;
      this.frameElapsed = 0;
      this.lastRafTime = null;
      this.lastError = null;
      this.lastWarning = null;
      this._emitState();
      if (!this.frames.length) return Promise.resolve(null);
      return this._ensureDecoded(this.index, generation, true)
        .then((decoded) => {
          if (generation !== this.generation || this.destroyed) return null;
          if (decoded) this._display(this.index, decoded, generation);
          this._prefetchAround(this.index, generation);
          if (this.playing) this._schedule();
          return decoded;
        })
        .catch((error) => {
          if (generation !== this.generation || this.destroyed || error && error.name === "AbortError") return null;
          this._reportError(error);
          return null;
        });
    }

    setSpeed(fps) {
      this._assertUsable();
      this.speed = clamp(finiteNumber(fps, this.speed), 0.1, 120);
      this.frameElapsed = 0;
      this._cancelScheduled();
      if (this.playing) this._schedule();
      this._emitState();
      return this;
    }

    setTimeMode(mode) {
      this._assertUsable();
      if (mode !== "uniform" && mode !== "observed") throw new TypeError("Playback time mode must be uniform or observed");
      this.timeMode = mode;
      this.frameElapsed = 0;
      this.lastWarning = null;
      this._cancelScheduled();
      if (this.playing) this._schedule();
      this._emitState();
      return this;
    }

    destroy() {
      if (this.destroyed) return;
      this.playing = false;
      this.destroyed = true;
      this.generation += 1;
      this._cancelScheduled();
      this._cancelPending();
      this._clearCache();
      this.frames = [];
      this.index = 0;
      this._emitState();
      this.render = null;
      this.onStateChange = null;
      this.onError = null;
      this.target = null;
    }

    getState() {
      const duration = this.frames.length ? this._durationFor(this.index) : 0;
      return {
        index: this.index,
        frame_count: this.frames.length,
        frameCount: this.frames.length,
        playing: this.playing,
        speed: this.speed,
        fps: this.speed,
        time_mode: this.timeMode,
        timeMode: this.timeMode,
        last_error: this.lastError,
        lastError: this.lastError,
        last_warning: this.lastWarning,
        lastWarning: this.lastWarning,
        current_duration_ms: duration,
        pending_decodes: this.pending.size,
        active_decodes: this.activeDecodes,
        decoded_cache_size: this.cache.size,
        reduced_motion: this.reducedMotion,
        reducedMotion: this.reducedMotion,
        destroyed: this.destroyed,
        frame: this.frames[this.index] || null,
      };
    }

    _assertUsable() {
      if (this.destroyed) throw new Error("Playback controller has been destroyed");
    }

    _schedule() {
      if (!this.playing || this.destroyed || this.rafId !== null) return;
      this.rafId = this.raf((timestamp) => {
        this.rafId = null;
        this._tick(finiteNumber(timestamp, this.now()));
      });
    }

    _tick(timestamp) {
      if (!this.playing || this.destroyed || this.frames.length < 2) return;
      if (this.lastRafTime === null) this.lastRafTime = timestamp;
      const delta = clamp(timestamp - this.lastRafTime, 0, 1000);
      this.lastRafTime = timestamp;
      this.frameElapsed += delta;
      let duration = this._durationFor(this.index);
      // A stalled tab can produce one very large callback; advance at most
      // one complete cycle per tick and retain the remainder for fairness.
      if (this.frameElapsed >= duration) {
        this.frameElapsed -= duration;
        this._advance((this.index + 1) % this.frames.length);
        duration = this._durationFor(this.index);
        if (this.frameElapsed >= duration) this.frameElapsed = 0;
      }
      this._schedule();
    }

    _advance(nextIndex) {
      this.index = nextIndex;
      this.frameElapsed = 0;
      const generation = this.generation;
      this._ensureDecoded(nextIndex, generation, true)
        .then((decoded) => {
          if (decoded && generation === this.generation && this.index === nextIndex && !this.destroyed) {
            this._display(nextIndex, decoded, generation);
          }
        })
        .catch((error) => {
          if (generation === this.generation && !(error && error.name === "AbortError")) this._reportError(error);
        });
      this._prefetchAround(nextIndex, generation);
      this._emitState();
    }

    _durationFor(index) {
      const uniform = Math.max(this.minFrameMs, 1000 / this.speed);
      if (this.timeMode !== "observed" || this.frames.length < 2) return uniform;
      const current = frameDate(this.frames[index]);
      const next = frameDate(this.frames[(index + 1) % this.frames.length]);
      // There is no meaningful wraparound source timestamp.  Treat the final
      // frame as a uniform frame unless the sequence explicitly wraps in time.
      if (index === this.frames.length - 1 && next !== null && current !== null && next <= current) return uniform;
      if (current === null || next === null || next <= current) return uniform;
      const rawGap = next - current;
      const cappedGap = Math.min(rawGap, this.maxGapMs);
      if (rawGap > this.maxGapMs) {
        this.lastWarning = `Observed gap of ${Math.round(rawGap / 1000)}s capped at ${Math.round(this.maxGapMs / 1000)}s`;
      }
      // speed=6 is the default real-time scaling for observed playback;
      // higher values make the same observations play faster.
      return Math.max(this.minFrameMs, cappedGap * (DEFAULT_SPEED / this.speed));
    }

    _prefetchAround(index, generation) {
      if (!this.frames.length || generation !== this.generation || this.destroyed) return;
      const candidates = [
        index,
        (index + 1) % this.frames.length,
        (index + 2) % this.frames.length,
        (index - 1 + this.frames.length) % this.frames.length,
      ];
      candidates.slice(0, this.maxCache).forEach((candidate, offset) => {
        this._ensureDecoded(candidate, generation, offset === 0).catch((error) => {
          if (generation === this.generation && !(error && error.name === "AbortError") && candidate === this.index) this._reportError(error);
        });
      });
    }

    _ensureDecoded(index, generation, priority) {
      if (generation !== this.generation || this.destroyed || !this.frames[index]) return Promise.resolve(null);
      if (this.cache.has(index)) {
        const decoded = this.cache.get(index);
        this.cache.delete(index);
        this.cache.set(index, decoded);
        return Promise.resolve(decoded);
      }
      const existing = this.pending.get(index);
      if (existing) return existing.promise;

      let resolvePromise;
      let rejectPromise;
      const promise = new Promise((resolve, reject) => { resolvePromise = resolve; rejectPromise = reject; });
      const item = {
        index,
        generation,
        promise,
        resolve: resolvePromise,
        reject: rejectPromise,
        controller: typeof AbortController === "function" ? new AbortController() : null,
        started: false,
        cancelled: false,
      };
      this.pending.set(index, item);
      if (priority) this.queue.unshift(item); else this.queue.push(item);
      this._pumpQueue();
      return promise;
    }

    _pumpQueue() {
      while (!this.destroyed && this.activeDecodes < this.maxConcurrent && this.queue.length) {
        const item = this.queue.shift();
        if (!item || item.cancelled || item.generation !== this.generation || this.pending.get(item.index) !== item) continue;
        item.started = true;
        this.activeDecodes += 1;
        this._decodeItem(item);
      }
    }

    async _decodeItem(item) {
      let decoded = null;
      let error = null;
      try {
        throwIfAborted(item.controller && item.controller.signal);
        decoded = await this._decodeRecord(this.frames[item.index], item.controller && item.controller.signal);
        throwIfAborted(item.controller && item.controller.signal);
      } catch (caught) {
        error = caught;
      }
      this.activeDecodes = Math.max(0, this.activeDecodes - 1);
      if (this.pending.get(item.index) === item) this.pending.delete(item.index);
      if (item.cancelled || item.generation !== this.generation || this.destroyed) {
        this._releaseDecoded(decoded);
        item.resolve(null);
      } else if (error) {
        item.reject(error);
      } else {
        this._cacheSet(item.index, decoded);
        item.resolve(decoded);
      }
      this._pumpQueue();
      this._emitState();
    }

    async _decodeRecord(frame, signal) {
      const urls = uniqueUrls(frame);
      if (!urls.length) throw new Error(`Frame ${frame && frame.filename ? frame.filename : "<unknown>"} has no URL`);
      let lastError = null;
      for (const url of urls) {
        try {
          throwIfAborted(signal);
          const value = this.decodeFrame
            ? await this.decodeFrame(frame, { url, signal })
            : await defaultDecode(url, { signal });
          throwIfAborted(signal);
          const normalized = value && typeof value === "object" ? { ...value } : { value };
          if (!normalized.url) normalized.url = url;
          if (normalized.objectUrl) this.ownedObjectUrls.add(normalized.objectUrl);
          return normalized;
        } catch (error) {
          if (error && error.name === "AbortError") throw error;
          lastError = error;
        }
      }
      throw lastError || new Error(`Unable to decode frame ${frame.filename || "<unknown>"}`);
    }

    _cacheSet(index, decoded) {
      if (!decoded) return;
      const previous = this.cache.get(index);
      if (previous && previous !== decoded) this._releaseDecoded(previous);
      this.cache.delete(index);
      this.cache.set(index, decoded);
      while (this.cache.size > this.maxCache) {
        const oldest = this.cache.keys().next().value;
        const value = this.cache.get(oldest);
        this.cache.delete(oldest);
        this._releaseDecoded(value);
      }
    }

    _display(index, decoded, generation) {
      if (generation !== this.generation || this.destroyed || index !== this.index) return;
      try {
        if (this.render) {
          this.render(decoded, this.frames[index], index, {
            reducedMotion: this.reducedMotion,
            crossfade: !this.reducedMotion,
          });
        } else if (this.target) {
          const source = decoded && (decoded.url || decoded.src || decoded.element && decoded.element.src);
          if (source) this.target.src = source;
        }
        this.lastError = null;
        this._emitState();
      } catch (error) {
        this._reportError(error);
      }
    }

    _reportError(error) {
      this.lastError = error instanceof Error ? error.message : String(error);
      if (this.onError) {
        try { this.onError(error, this.getState()); } catch (_) { /* observer errors do not stop playback */ }
      }
      this._emitState();
    }

    _cancelScheduled() {
      if (this.rafId !== null) {
        this.cancelRaf(this.rafId);
        this.rafId = null;
      }
    }

    _cancelPending() {
      const items = [...this.pending.values()];
      this.queue = [];
      for (const item of items) {
        item.cancelled = true;
        if (item.started) {
          this.pending.delete(item.index);
          if (item.controller) item.controller.abort();
        } else {
          this.pending.delete(item.index);
          item.resolve(null);
        }
      }
    }

    _releaseDecoded(decoded) {
      if (!decoded || typeof decoded !== "object") return;
      if (decoded.objectUrl && this.ownedObjectUrls.has(decoded.objectUrl)) {
        if (root && root.URL && typeof root.URL.revokeObjectURL === "function") root.URL.revokeObjectURL(decoded.objectUrl);
        this.ownedObjectUrls.delete(decoded.objectUrl);
      }
    }

    _clearCache() {
      for (const decoded of this.cache.values()) this._releaseDecoded(decoded);
      this.cache.clear();
      for (const objectUrl of this.ownedObjectUrls) {
        if (root && root.URL && typeof root.URL.revokeObjectURL === "function") root.URL.revokeObjectURL(objectUrl);
      }
      this.ownedObjectUrls.clear();
    }

    _emitState() {
      if (!this.onStateChange) return;
      try { this.onStateChange(this.getState()); } catch (_) { /* state observers are non-critical */ }
    }
  }

  return { create: (options) => new PlaybackController(options) };
});
