/* RadarVault UI */
(() => {
  const state = {
    radars: [],
    selected: null,
    markers: new Map(),
    frames: [],
    map: null,
    overlay: null,
    overlayFrames: [],
    overlayIndex: 0,
    overlayPlaying: false,
    playback: null,
    playbackMode: "uniform",
    statusByRadar: new Map(),
    lastExportUrl: null,
  };

  const el = {
    selected: document.getElementById("selected"),
    btnStart: document.getElementById("btn-start"),
    btnStop: document.getElementById("btn-stop"),
    actionMsg: document.getElementById("action-msg"),
    statusList: document.getElementById("status-list"),
    previewImg: document.getElementById("preview-img"),
    previewEmpty: document.getElementById("preview-empty"),
    scrubber: document.getElementById("scrubber"),
    scrub: document.getElementById("scrub"),
    scrubLabel: document.getElementById("scrub-label"),
    scrubCount: document.getElementById("scrub-count"),
    btnOverlayPlay: document.getElementById("btn-overlay-play"),
    btnOverlayStop: document.getElementById("btn-overlay-stop"),
    overlayOpacity: document.getElementById("overlay-opacity"),
    overlayHud: document.getElementById("overlay-hud"),
    hudPause: document.getElementById("hud-pause"),
    hudLabel: document.getElementById("hud-label"),
    exportForm: document.getElementById("export-form"),
    exportStart: document.getElementById("export-start"),
    exportEnd: document.getElementById("export-end"),
    exportFps: document.getElementById("export-fps"),
    btnExport: document.getElementById("btn-export"),
    exportMsg: document.getElementById("export-msg"),
    exportLink: document.getElementById("export-link"),
    btnOverlayExport: document.getElementById("btn-overlay-export"),
    radarSearch: document.getElementById("radar-search"),
    filterSupported: document.getElementById("radar-filter-supported"),
    filterCached: document.getElementById("radar-filter-cached"),
    filterCount: document.getElementById("radar-filter-count"),
    statusSummary: document.getElementById("status-summary"),
    statusActive: document.getElementById("status-active"),
    statusCached: document.getElementById("status-cached"),
    statusCachedList: document.getElementById("status-cached-list"),
    playbackSpeed: document.getElementById("playback-speed"),
    playbackTimeMode: document.getElementById("playback-time-mode"),
    timezoneMode: document.getElementById("timezone-mode"),
  };

  function showMsg(node, text, isError = false) {
    node.hidden = !text;
    node.textContent = text || "";
    node.classList.toggle("error", Boolean(isError));
  }

  function canArchive(radar) {
    if (!radar) return false;
    if (typeof radar.supports_archive === "boolean") return radar.supports_archive;
    return radar.supports_sr_bref !== false;
  }

  function iconColor(radar, selected) {
    if (selected) return "#f0b429";
    if (!canArchive(radar)) return "#6b7a73";
    if (radar.kind === "tdwr" || radar.product === "bref1" || radar.product === "brefl") {
      return "#5b9fd4";
    }
    return "#3dba8a";
  }

  function makeIcon(radar, selected) {
    const color = iconColor(radar, selected);
    const size = selected ? 16 : 11;
    return L.divIcon({
      className: "",
      html: `<div style="width:${size}px;height:${size}px;border-radius:50%;background:${color};border:2px solid rgba(255,255,255,0.85);box-shadow:0 0 0 2px rgba(0,0,0,0.25)"></div>`,
      iconSize: [size, size],
      iconAnchor: [size / 2, size / 2],
    });
  }

  function pad2(n) {
    return String(n).padStart(2, "0");
  }

  /** Format a Date into datetime-local using UTC components (inputs are labeled UTC). */
  function toUtcInput(d) {
    return `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())}T${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`;
  }

  function utcInputToIso(value) {
    if (!value) return null;
    return value.length === 16 ? `${value}:00Z` : `${value}Z`;
  }

  function formatFrameTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: "short",
      timeStyle: "medium",
      timeZone: el.timezoneMode.value === "local" ? undefined : "UTC",
    }).format(date) + (el.timezoneMode.value === "local" ? " local" : " UTC");
  }

  function setRadarVisibility(radar, visible) {
    const marker = state.markers.get(radar.id);
    if (!marker || !state.map) return;
    if (visible && !state.map.hasLayer(marker)) marker.addTo(state.map);
    if (!visible && state.map.hasLayer(marker)) state.map.removeLayer(marker);
  }

  function applyRadarFilters() {
    const query = (el.radarSearch.value || "").trim().toLowerCase();
    const supportedOnly = el.filterSupported.checked;
    const cachedOnly = el.filterCached.checked;
    let visibleCount = 0;
    state.radars.forEach((radar) => {
      const haystack = `${radar.id} ${radar.name || ""}`.toLowerCase();
      const status = state.statusByRadar.get(radar.id);
      const visible = (!query || haystack.includes(query))
        && (!supportedOnly || canArchive(radar))
        && (!cachedOnly || Boolean(status && Number(status.frame_count) > 0));
      setRadarVisibility(radar, visible);
      if (visible) visibleCount += 1;
    });
    el.filterCount.textContent = `${visibleCount} of ${state.radars.length} radars shown`;
  }

  function setExportWindowFromFrames(frames) {
    if (!frames.length) return;
    const first = new Date(frames[0].utc);
    const last = new Date(frames[frames.length - 1].utc);
    el.exportStart.value = toUtcInput(new Date(first.getTime() - 60 * 1000));
    el.exportEnd.value = toUtcInput(new Date(last.getTime() + 60 * 1000));
  }

  function selectRadar(radar) {
    const prev = state.selected;
    state.selected = radar;
    if (prev && state.markers.has(prev.id)) {
      state.markers.get(prev.id).setIcon(makeIcon(prev, false));
    }
    if (radar && state.markers.has(radar.id)) {
      state.markers.get(radar.id).setIcon(makeIcon(radar, true));
    }

    if (!radar) {
      el.selected.classList.add("empty");
      el.selected.textContent = "Click a marker on the map";
      el.btnStart.disabled = true;
      el.btnStop.disabled = true;
      el.btnExport.disabled = true;
      el.btnOverlayPlay.disabled = true;
      clearPreview();
      return;
    }

    el.selected.classList.remove("empty");
    let support;
    if (!canArchive(radar)) {
      support = `<div style="color:#e36d6d;margin-top:0.35rem">No archive product available</div>`;
    } else if (radar.kind === "tdwr") {
      support = `<div style="color:#5b9fd4;margin-top:0.35rem">TDWR · product ${radar.product}</div>`;
    } else {
      support = `<div style="color:#8fa399;margin-top:0.35rem">WSR-88D · product ${radar.product || "sr_bref"}</div>`;
    }
    el.selected.innerHTML = `<strong>${radar.id}</strong>${radar.name}<br/><span style="color:#8fa399">${radar.lat.toFixed(3)}, ${radar.lon.toFixed(3)}</span>${support}`;

    el.btnStart.disabled = !canArchive(radar);
    el.btnStop.disabled = false;
    el.btnExport.disabled = false;
    showMsg(el.actionMsg, "");
    loadFrames(radar.id);
  }

  function clearPreview() {
    el.previewImg.hidden = true;
    el.previewImg.removeAttribute("src");
    el.previewEmpty.hidden = false;
    el.scrubber.hidden = true;
    el.btnOverlayPlay.disabled = true;
    state.frames = [];
  }

  async function loadFrames(radarId) {
    try {
      const frames = await fetchJSON(`/api/cache/${radarId}/frames?limit=500`);
      state.frames = frames;
      if (!frames.length) {
        clearPreview();
        return;
      }
      el.previewEmpty.hidden = true;
      el.scrubber.hidden = false;
      el.scrub.min = 0;
      el.scrub.max = String(frames.length - 1);
      el.scrub.value = String(frames.length - 1);
      el.scrubCount.textContent = `${frames.length} frames`;
      el.btnOverlayPlay.disabled = false;
      setExportWindowFromFrames(frames);
      showFrame(frames.length - 1);
    } catch {
      clearPreview();
    }
  }

  function showFrame(index) {
    const frame = state.frames[index];
    if (!frame || !state.selected) return;
    el.previewImg.hidden = false;
    el.previewImg.src = frame.preview_url || frame.url || `/api/cache/${state.selected.id}/frame/${encodeURIComponent(frame.filename)}`;
    el.scrubLabel.textContent = formatFrameTime(frame.observed_at || frame.utc);
  }

  async function fetchJSON(url, options) {
    const res = await fetch(url, options);
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
    if (!res.ok) {
      const detail = data && (data.detail || data.message) || res.statusText;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return data;
  }

  function createPlayback(options = {}) {
    if (!window.RadarVaultPlayback || typeof window.RadarVaultPlayback.create !== "function") {
      throw new Error("RadarVaultPlayback failed to load");
    }
    return window.RadarVaultPlayback.create(options);
  }

  function stopOverlayPlayback() {
    if (state.playback) state.playback.pause();
    state.overlayPlaying = false;
    if (el.hudPause) el.hudPause.textContent = "Play";
  }

  function clearOverlay() {
    stopOverlayPlayback();
    if (state.playback) state.playback.destroy();
    state.playback = null;
    if (state.overlay) {
      state.map.removeLayer(state.overlay);
      state.overlay = null;
    }
    state.overlayFrames = [];
    state.overlayIndex = 0;
    el.overlayHud.hidden = true;
    el.btnOverlayStop.disabled = true;
  }

  function setOverlayFrame(index) {
    if (!state.overlayFrames.length || !state.overlay) return;
    const i = ((index % state.overlayFrames.length) + state.overlayFrames.length) % state.overlayFrames.length;
    state.overlayIndex = i;
    const frame = state.overlayFrames[i];
    const url = frame.preview_url || frame.url;
    const img = state.overlay.getElement && state.overlay.getElement();
    if (img) {
      img.src = url;
    } else {
      state.overlay.setUrl(url);
    }
    el.hudLabel.textContent = `${formatFrameTime(frame.observed_at || frame.utc)} · ${i + 1}/${state.overlayFrames.length}`;
  }

  function startOverlayPlayback(fps = Number(el.playbackSpeed.value) || 6) {
    stopOverlayPlayback();
    if (state.overlayFrames.length < 2) {
      state.overlayPlaying = false;
      el.hudPause.textContent = "Play";
      el.hudLabel.textContent = state.overlayFrames[0]
        ? `${formatFrameTime(state.overlayFrames[0].observed_at || state.overlayFrames[0].utc)} · 1/1 (need 2+ frames to animate)`
        : "—";
      return;
    }
    const rate = Math.min(Math.max(Number(fps) || 6, 1), 30);
    if (state.playback) {
      state.playback.setSpeed(rate);
      state.playback.play();
    }
    state.overlayPlaying = true;
    el.hudPause.textContent = "Pause";
  }

  async function playOverlayFromCache(fps) {
    if (!state.selected) return;
    showMsg(el.actionMsg, "Loading map overlay…");
    try {
      // Prefer all cached frames for overlay so a tight/wrong time window
      // cannot silently empty the playlist. Export still uses the form range.
      let data = await fetchJSON(`/api/cache/${state.selected.id}/overlay?limit=500`);
      if (!data.frames.length) {
        throw new Error("No cached frames yet — start archiving first");
      }

      if (!data.bounds || !Array.isArray(data.bounds) || data.bounds.length !== 2) {
        throw new Error("Missing geographic bounds for overlay");
      }

      clearOverlay();
      const bounds = L.latLngBounds(data.bounds);
      if (!bounds.isValid()) {
        throw new Error("Invalid overlay bounds");
      }

      const opacity = Number(el.overlayOpacity.value) / 100;
      state.overlayFrames = data.frames;

      state.overlay = L.imageOverlay(data.frames[0].preview_url || data.frames[0].url, bounds, {
        opacity,
        interactive: false,
        className: "radar-overlay",
        zIndex: 450,
      }).addTo(state.map);

      state.map.fitBounds(bounds, { padding: [24, 24], maxZoom: 9 });
      el.overlayHud.hidden = false;
      el.btnOverlayStop.disabled = false;
      state.playback = createPlayback({
        onFrame: (_decoded, _record, index) => setOverlayFrame(index),
        speed: Number(el.playbackSpeed.value) || 6,
        timeMode: state.playbackMode,
      });
      state.playback.load(data.frames, { index: 0, timeMode: state.playbackMode });
      startOverlayPlayback(fps || Number(el.playbackSpeed.value) || 6);
      showMsg(
        el.actionMsg,
        `Map overlay · ${data.frames.length} frames (${data.product || "radar"})`
      );
    } catch (err) {
      showMsg(el.actionMsg, err.message, true);
    }
  }

  async function refreshStatus() {
    try {
      const status = await fetchJSON("/api/cache/status");
      const radars = Object.values(status.radars || {});
      state.statusByRadar = new Map(radars.map((r) => [r.radar_id, r]));
      const active = radars.filter((r) => r.running);
      const cached = radars.filter((r) => Number(r.frame_count) > 0);
      el.statusSummary.textContent = `${active.length} active archive${active.length === 1 ? "" : "s"} · ${cached.length} cached radar${cached.length === 1 ? "" : "s"}`;
      el.statusActive.hidden = false;
      el.statusList.innerHTML = active.length ? active
        .map((r) => {
          const mb = ((r.disk_bytes || 0) / (1024 * 1024)).toFixed(2);
          const badge = r.running
            ? `<span class="badge on">running</span>`
            : `<span class="badge off">idle</span>`;
          return `<div class="status-card"><div><span class="id">${r.radar_id}</span>${badge}</div>
            <div class="meta">frames: ${r.frame_count || 0} · ${mb} MB<br/>last: ${r.last_frame_utc || "—"}
            ${r.last_error ? `<br/><span style="color:#e36d6d">${r.last_error}</span>` : ""}</div></div>`;
        }).join("") : "No active archives yet.";
      el.statusCached.hidden = !cached.length;
      el.statusCachedList.innerHTML = cached.map((r) => `<div class="status-card"><div><span class="id">${r.radar_id}</span></div><div class="meta">${r.frame_count} frames · ${(Number(r.disk_bytes || 0) / (1024 * 1024)).toFixed(2)} MB</div></div>`).join("");
      applyRadarFilters();

      // Frame lists are loaded on radar selection and after explicit actions;
      // status polling must not repeatedly scan an archive directory.
    } catch (err) {
      el.statusList.textContent = `Status error: ${err.message}`;
    }
  }

  function defaultExportWindow() {
    const end = new Date();
    const start = new Date(end.getTime() - 6 * 60 * 60 * 1000);
    el.exportStart.value = toUtcInput(start);
    el.exportEnd.value = toUtcInput(end);
  }

  async function initMap() {
    state.map = L.map("map", { zoomControl: true }).setView([39.5, -98.35], 4);
    L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
      attribution: "&copy; OpenStreetMap &copy; CARTO",
      maxZoom: 18,
    }).addTo(state.map);

    const radars = await fetchJSON("/api/radars");
    state.radars = radars;
    radars.forEach((radar) => {
      const marker = L.marker([radar.lat, radar.lon], {
        icon: makeIcon(radar, false),
        title: `${radar.name} (${radar.id})`,
      });
      const kind = radar.kind === "tdwr" ? "TDWR" : radar.kind === "wsr88d" ? "WSR-88D" : "n/a";
      marker.bindTooltip(`${radar.name} · ${radar.id} · ${kind}`, { direction: "top", offset: [0, -6] });
      marker.on("click", () => selectRadar(radar));
      marker.addTo(state.map);
      state.markers.set(radar.id, marker);
    });
  }

  el.btnStart.addEventListener("click", async () => {
    if (!state.selected) return;
    try {
      await fetchJSON(`/api/cache/${state.selected.id}/start`, { method: "POST" });
      showMsg(el.actionMsg, `Archiving ${state.selected.id}…`);
      refreshStatus();
    } catch (err) {
      showMsg(el.actionMsg, err.message, true);
    }
  });

  el.btnStop.addEventListener("click", async () => {
    if (!state.selected) return;
    try {
      await fetchJSON(`/api/cache/${state.selected.id}/stop`, { method: "POST" });
      showMsg(el.actionMsg, `Stopped ${state.selected.id}`);
      refreshStatus();
    } catch (err) {
      showMsg(el.actionMsg, err.message, true);
    }
  });

  el.scrub.addEventListener("input", () => {
    showFrame(Number(el.scrub.value));
  });

  el.btnOverlayPlay.addEventListener("click", () => {
    playOverlayFromCache(Number(el.playbackSpeed.value) || 6);
  });

  el.btnOverlayStop.addEventListener("click", () => {
    clearOverlay();
    showMsg(el.actionMsg, "Overlay cleared");
  });

  el.overlayOpacity.addEventListener("input", () => {
    if (state.overlay) {
      state.overlay.setOpacity(Number(el.overlayOpacity.value) / 100);
    }
  });

  el.hudPause.addEventListener("click", () => {
    if (state.overlayPlaying) {
      stopOverlayPlayback();
    } else {
      startOverlayPlayback(Number(el.playbackSpeed.value) || 6);
    }
  });

  el.radarSearch.addEventListener("input", applyRadarFilters);
  el.filterSupported.addEventListener("change", applyRadarFilters);
  el.filterCached.addEventListener("change", applyRadarFilters);
  el.playbackSpeed.addEventListener("change", () => {
    const rate = Number(el.playbackSpeed.value) || 6;
    if (state.playback) state.playback.setSpeed(rate);
  });
  el.playbackTimeMode.addEventListener("change", () => {
    state.playbackMode = el.playbackTimeMode.value === "observed" ? "observed" : "uniform";
    if (state.playback) state.playback.setTimeMode(state.playbackMode);
  });
  el.timezoneMode.addEventListener("change", () => {
    if (state.frames.length) showFrame(Number(el.scrub.value));
    if (state.overlayFrames.length) setOverlayFrame(state.overlayIndex);
  });

  el.exportForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (!state.selected) return;
    el.btnExport.disabled = true;
    el.exportLink.hidden = true;
    el.btnOverlayExport.hidden = true;
    showMsg(el.exportMsg, "Generating video…");
    try {
      const result = await fetchJSON("/api/videos/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          radar_id: state.selected.id,
          start: utcInputToIso(el.exportStart.value),
          end: utcInputToIso(el.exportEnd.value),
          fps: Number(el.exportFps.value) || 15,
        }),
      });
      let status = result;
      while (status.state === "queued" || status.state === "running") {
        showMsg(el.exportMsg, `${status.message || "Exporting"} · ${Math.round((status.progress || 0) * 100)}%`);
        await new Promise((resolve) => window.setTimeout(resolve, 500));
        status = await fetchJSON(`/api/videos/jobs/${encodeURIComponent(result.job_id)}`);
      }
      if (status.state !== "complete") throw new Error(status.error || status.message || "Video export failed");
      showMsg(el.exportMsg, `Complete · ${status.filename}`);
      el.exportLink.hidden = false;
      el.exportLink.href = status.download_url;
      el.exportLink.textContent = `Download ${status.filename}`;
      el.btnOverlayExport.hidden = false;
      state.lastExportUrl = status.download_url;
    } catch (err) {
      showMsg(el.exportMsg, err.message, true);
    } finally {
      el.btnExport.disabled = !state.selected;
    }
  });

  el.btnOverlayExport.addEventListener("click", () => {
    playOverlayFromCache(Number(el.exportFps.value) || 6);
  });

  defaultExportWindow();
  initMap()
    .then(() => refreshStatus())
    .catch((err) => {
      el.selected.classList.remove("empty");
      el.selected.textContent = `Failed to load radars: ${err.message}`;
    });

  setInterval(refreshStatus, 4000);
})();
