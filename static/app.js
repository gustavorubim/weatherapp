/* RadarVault UI */
(() => {
  const state = {
    radars: [],
    selected: null,
    markers: new Map(),
    frames: [],
    allFrames: [],
    map: null,
    overlay: null,
    overlayFrames: [],
    overlayIndex: 0,
    overlayPlaying: false,
    playback: null,
    playbackMode: "uniform",
    statusByRadar: new Map(),
    lastExportUrl: null,
    libraryRadarId: null,
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
    tabArchive: document.getElementById("tab-archive"),
    tabLibrary: document.getElementById("tab-library"),
    panelArchive: document.getElementById("panel-archive"),
    panelLibrary: document.getElementById("panel-library"),
    playbackRange: document.getElementById("playback-range"),
    playbackStart: document.getElementById("playback-start"),
    playbackEnd: document.getElementById("playback-end"),
    btnPlaybackRange: document.getElementById("btn-playback-range"),
    librarySummary: document.getElementById("library-summary"),
    libraryList: document.getElementById("library-list"),
    libraryBefore: document.getElementById("library-before"),
    btnLibraryTrim: document.getElementById("btn-library-trim"),
    btnLibraryClear: document.getElementById("btn-library-clear"),
    libraryMsg: document.getElementById("library-msg"),
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
    return "#4a9fd8";
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

  function setPlaybackWindowFromFrames(frames) {
    if (!frames.length || !el.playbackStart || !el.playbackEnd) return;
    el.playbackStart.value = toUtcInput(new Date(frames[0].utc));
    el.playbackEnd.value = toUtcInput(new Date(frames[frames.length - 1].utc));
  }

  function filterFramesByPlaybackWindow(frames) {
    const startIso = utcInputToIso(el.playbackStart && el.playbackStart.value);
    const endIso = utcInputToIso(el.playbackEnd && el.playbackEnd.value);
    const startMs = startIso ? Date.parse(startIso) : null;
    const endMs = endIso ? Date.parse(endIso) : null;
    return frames.filter((frame) => {
      const ms = Date.parse(frame.observed_at || frame.utc);
      if (Number.isNaN(ms)) return true;
      if (startMs != null && ms < startMs) return false;
      if (endMs != null && ms > endMs) return false;
      return true;
    });
  }

  function applyPlaybackWindow() {
    const filtered = filterFramesByPlaybackWindow(state.allFrames);
    if (!filtered.length) {
      el.previewEmpty.hidden = false;
      el.previewEmpty.textContent = "No frames in the selected time window.";
      el.scrubber.hidden = true;
      el.btnOverlayPlay.disabled = true;
      state.frames = [];
      return;
    }
    el.previewEmpty.hidden = true;
    el.previewEmpty.textContent = "No frames cached for this radar.";
    el.scrubber.hidden = false;
    el.scrub.min = 0;
    el.scrub.max = String(filtered.length - 1);
    el.scrub.value = String(filtered.length - 1);
    el.scrubCount.textContent = `${filtered.length} of ${state.allFrames.length} frames`;
    el.btnOverlayPlay.disabled = false;
    state.frames = filtered;
    showFrame(filtered.length - 1);
  }

  function switchTab(name) {
    const archive = name === "archive";
    el.tabArchive.setAttribute("aria-selected", archive ? "true" : "false");
    el.tabLibrary.setAttribute("aria-selected", archive ? "false" : "true");
    el.panelArchive.hidden = !archive;
    el.panelLibrary.hidden = archive;
    if (!archive) refreshLibrary();
  }

  function formatBytes(bytes) {
    const value = Number(bytes) || 0;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(2)} MB`;
    return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  }

  function setLibrarySelection(radarId) {
    state.libraryRadarId = radarId || null;
    el.btnLibraryTrim.disabled = !radarId;
    el.btnLibraryClear.disabled = !radarId;
    el.libraryList.querySelectorAll(".library-card").forEach((card) => {
      card.classList.toggle("is-active", card.dataset.radarId === radarId);
    });
  }

  async function refreshLibrary() {
    try {
      const data = await fetchJSON("/api/storage/radars");
      const radars = (data.radars || []).filter((r) => Number(r.frame_count) > 0);
      el.librarySummary.textContent = `${radars.length} radar${radars.length === 1 ? "" : "s"} · ${data.frame_count || 0} frames · ${formatBytes(data.bytes)}`;
      if (!radars.length) {
        el.libraryList.innerHTML = `<p class="muted">No cached radars yet.</p>`;
        setLibrarySelection(null);
        return;
      }
      el.libraryList.innerHTML = radars.map((r) => {
        const first = r.first_utc ? formatFrameTime(r.first_utc) : "—";
        const last = r.last_utc ? formatFrameTime(r.last_utc) : "—";
        const firstInput = r.first_utc ? toUtcInput(new Date(r.first_utc)) : "";
        return `<div class="library-card" data-radar-id="${r.radar_id}" data-first-utc="${firstInput}" role="button" tabindex="0">
          <div><span class="id">${r.radar_id}</span></div>
          <div class="meta">${r.frame_count} frames · ${formatBytes(r.disk_bytes)}<br/>${first} → ${last}</div>
          <div class="actions">
            <button class="btn" type="button" data-action="select">Manage</button>
            <button class="btn" type="button" data-action="open">Open on map</button>
          </div>
        </div>`;
      }).join("");
      if (state.libraryRadarId && !radars.some((r) => r.radar_id === state.libraryRadarId)) {
        setLibrarySelection(null);
      } else if (state.libraryRadarId) {
        setLibrarySelection(state.libraryRadarId);
      } else if (state.selected && radars.some((r) => r.radar_id === state.selected.id)) {
        setLibrarySelection(state.selected.id);
      }
    } catch (err) {
      el.librarySummary.textContent = `Library error: ${err.message}`;
    }
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
      support = `<div style="color:#8b9aab;margin-top:0.35rem">WSR-88D · product ${radar.product || "sr_bref"}</div>`;
    }
    el.selected.innerHTML = `<strong>${radar.id}</strong>${radar.name}<br/><span style="color:#8b9aab">${radar.lat.toFixed(3)}, ${radar.lon.toFixed(3)}</span>${support}`;

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
    el.previewEmpty.textContent = "No frames cached for this radar.";
    el.scrubber.hidden = true;
    if (el.playbackRange) el.playbackRange.hidden = true;
    el.btnOverlayPlay.disabled = true;
    state.frames = [];
    state.allFrames = [];
  }

  async function loadFrames(radarId) {
    try {
      const frames = await fetchJSON(`/api/cache/${radarId}/frames?limit=500`);
      state.allFrames = frames;
      if (!frames.length) {
        clearPreview();
        return;
      }
      if (el.playbackRange) el.playbackRange.hidden = false;
      setPlaybackWindowFromFrames(frames);
      setExportWindowFromFrames(frames);
      applyPlaybackWindow();
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
      const params = new URLSearchParams({ limit: "500" });
      const startIso = utcInputToIso(el.playbackStart && el.playbackStart.value);
      const endIso = utcInputToIso(el.playbackEnd && el.playbackEnd.value);
      if (startIso) params.set("start", startIso);
      if (endIso) params.set("end", endIso);
      let data = await fetchJSON(`/api/cache/${state.selected.id}/overlay?${params.toString()}`);
      if (!data.frames.length) {
        throw new Error("No frames in the selected time window — adjust Start/End or archive more");
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

  el.tabArchive.addEventListener("click", () => switchTab("archive"));
  el.tabLibrary.addEventListener("click", () => switchTab("library"));

  if (el.btnPlaybackRange) {
    el.btnPlaybackRange.addEventListener("click", () => {
      applyPlaybackWindow();
      showMsg(el.actionMsg, `Preview window · ${state.frames.length} frames`);
    });
  }

  el.libraryList.addEventListener("click", (ev) => {
    const button = ev.target.closest("button[data-action]");
    const card = ev.target.closest(".library-card");
    if (!card) return;
    const radarId = card.dataset.radarId;
    if (button && button.dataset.action === "open") {
      const radar = state.radars.find((r) => r.id === radarId);
      if (radar) {
        switchTab("archive");
        selectRadar(radar);
        state.map.setView([radar.lat, radar.lon], Math.max(state.map.getZoom(), 7));
      }
      return;
    }
    setLibrarySelection(radarId);
    if (card.dataset.firstUtc) {
      el.libraryBefore.value = card.dataset.firstUtc;
    }
  });

  el.btnLibraryTrim.addEventListener("click", async () => {
    if (!state.libraryRadarId) return;
    const before = utcInputToIso(el.libraryBefore.value);
    if (!before) {
      showMsg(el.libraryMsg, "Choose a UTC time to delete before.", true);
      return;
    }
    if (!window.confirm(`Delete all ${state.libraryRadarId} frames before ${el.libraryBefore.value} UTC?`)) {
      return;
    }
    try {
      const result = await fetchJSON(`/api/cache/${state.libraryRadarId}/frames/delete`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ before }),
      });
      showMsg(el.libraryMsg, `Deleted ${result.deleted_count} frames · freed ${formatBytes(result.reclaimed_bytes)}`);
      await refreshLibrary();
      await refreshStatus();
      if (state.selected && state.selected.id === state.libraryRadarId) {
        await loadFrames(state.selected.id);
      }
    } catch (err) {
      showMsg(el.libraryMsg, err.message, true);
    }
  });

  el.btnLibraryClear.addEventListener("click", async () => {
    if (!state.libraryRadarId) return;
    if (!window.confirm(`Delete ALL cached frames for ${state.libraryRadarId}? This cannot be undone.`)) {
      return;
    }
    try {
      const result = await fetchJSON(`/api/cache/${state.libraryRadarId}`, { method: "DELETE" });
      showMsg(el.libraryMsg, `Cleared ${result.deleted_count} frames · freed ${formatBytes(result.reclaimed_bytes)}`);
      await refreshLibrary();
      await refreshStatus();
      if (state.selected && state.selected.id === state.libraryRadarId) {
        clearOverlay();
        await loadFrames(state.selected.id);
      }
      setLibrarySelection(null);
    } catch (err) {
      showMsg(el.libraryMsg, err.message, true);
    }
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
