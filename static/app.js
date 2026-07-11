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
    overlayTimer: null,
    overlayPlaying: false,
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
      const frames = await fetchJSON(`/api/cache/${radarId}/frames`);
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
      el.btnOverlayPlay.disabled = frames.length < 1;
      showFrame(frames.length - 1);
    } catch {
      clearPreview();
    }
  }

  function showFrame(index) {
    const frame = state.frames[index];
    if (!frame || !state.selected) return;
    el.previewImg.hidden = false;
    el.previewImg.src = `/api/cache/${state.selected.id}/frame/${encodeURIComponent(frame.filename)}?t=${Date.now()}`;
    el.scrubLabel.textContent = frame.utc;
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

  function stopOverlayTimer() {
    if (state.overlayTimer) {
      clearInterval(state.overlayTimer);
      state.overlayTimer = null;
    }
    state.overlayPlaying = false;
    el.hudPause.textContent = "Play";
  }

  function clearOverlay() {
    stopOverlayTimer();
    if (state.overlay) {
      state.map.removeLayer(state.overlay);
      state.overlay = null;
    }
    state.overlayFrames = [];
    el.overlayHud.hidden = true;
    el.btnOverlayStop.disabled = true;
  }

  function setOverlayFrame(index) {
    if (!state.overlayFrames.length || !state.overlay) return;
    const i = ((index % state.overlayFrames.length) + state.overlayFrames.length) % state.overlayFrames.length;
    state.overlayIndex = i;
    const frame = state.overlayFrames[i];
    state.overlay.setUrl(`${frame.url}?t=${Date.now()}`);
    el.hudLabel.textContent = `${frame.utc} · ${i + 1}/${state.overlayFrames.length}`;
  }

  function startOverlayPlayback(fps) {
    stopOverlayTimer();
    if (state.overlayFrames.length < 2) {
      state.overlayPlaying = false;
      el.hudPause.textContent = "Play";
      return;
    }
    const interval = Math.max(40, Math.round(1000 / (fps || 8)));
    state.overlayPlaying = true;
    el.hudPause.textContent = "Pause";
    state.overlayTimer = setInterval(() => {
      setOverlayFrame(state.overlayIndex + 1);
    }, interval);
  }

  async function playOverlayFromCache(fps) {
    if (!state.selected) return;
    showMsg(el.actionMsg, "Loading map overlay…");
    try {
      const start = localInputToIso(el.exportStart.value);
      const end = localInputToIso(el.exportEnd.value);
      const qs = new URLSearchParams();
      if (start) qs.set("start", start);
      if (end) qs.set("end", end);
      const data = await fetchJSON(`/api/cache/${state.selected.id}/overlay?${qs}`);
      if (!data.frames.length) throw new Error("No frames in selected time range");

      clearOverlay();
      const bounds = L.latLngBounds(data.bounds);
      const opacity = Number(el.overlayOpacity.value) / 100;
      state.overlayFrames = data.frames;
      state.overlay = L.imageOverlay(data.frames[0].url, bounds, {
        opacity,
        interactive: false,
        className: "radar-overlay",
      }).addTo(state.map);
      state.map.fitBounds(bounds.pad(0.05));
      el.overlayHud.hidden = false;
      el.btnOverlayStop.disabled = false;
      setOverlayFrame(0);
      startOverlayPlayback(fps || Number(el.exportFps.value) || 8);
      showMsg(el.actionMsg, `Map overlay · ${data.frames.length} frames (${data.product})`);
    } catch (err) {
      showMsg(el.actionMsg, err.message, true);
    }
  }

  async function refreshStatus() {
    try {
      const status = await fetchJSON("/api/cache/status");
      const radars = Object.values(status.radars || {});
      if (!radars.length) {
        el.statusList.textContent = "No active archives yet.";
        return;
      }
      el.statusList.innerHTML = radars
        .map((r) => {
          const mb = ((r.disk_bytes || 0) / (1024 * 1024)).toFixed(2);
          const badge = r.running
            ? `<span class="badge on">running</span>`
            : `<span class="badge off">idle</span>`;
          return `<div class="status-card"><div><span class="id">${r.radar_id}</span>${badge}</div>
            <div class="meta">frames: ${r.frame_count || 0} · ${mb} MB<br/>last: ${r.last_frame_utc || "—"}
            ${r.last_error ? `<br/><span style="color:#e36d6d">${r.last_error}</span>` : ""}</div></div>`;
        })
        .join("");

      if (state.selected) {
        const live = status.radars[state.selected.id];
        if (live && live.running) {
          loadFrames(state.selected.id);
        }
      }
    } catch (err) {
      el.statusList.textContent = `Status error: ${err.message}`;
    }
  }

  function defaultExportWindow() {
    const end = new Date();
    const start = new Date(end.getTime() - 6 * 60 * 60 * 1000);
    const toLocalInput = (d) => {
      const pad = (n) => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    };
    el.exportStart.value = toLocalInput(start);
    el.exportEnd.value = toLocalInput(end);
  }

  function localInputToIso(value) {
    if (!value) return null;
    return value.length === 16 ? `${value}:00Z` : `${value}Z`;
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
    playOverlayFromCache(Number(el.exportFps.value) || 8);
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
      stopOverlayTimer();
    } else {
      startOverlayPlayback(Number(el.exportFps.value) || 8);
    }
  });

  el.exportForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (!state.selected) return;
    el.btnExport.disabled = true;
    el.exportLink.hidden = true;
    el.btnOverlayExport.hidden = true;
    showMsg(el.exportMsg, "Generating video…");
    try {
      const result = await fetchJSON("/api/videos/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          radar_id: state.selected.id,
          start: localInputToIso(el.exportStart.value),
          end: localInputToIso(el.exportEnd.value),
          fps: Number(el.exportFps.value) || 15,
        }),
      });
      showMsg(el.exportMsg, `Complete · ${(result.bytes / (1024 * 1024)).toFixed(2)} MB`);
      el.exportLink.hidden = false;
      el.exportLink.href = result.download_url;
      el.exportLink.textContent = `Download ${result.filename}`;
      el.btnOverlayExport.hidden = false;
      state.lastExportUrl = result.download_url;
    } catch (err) {
      showMsg(el.exportMsg, err.message, true);
    } finally {
      el.btnExport.disabled = !state.selected;
    }
  });

  // After export, play the same frame range as a georeferenced overlay
  // (MP4 itself isn't geo-aligned; cached frames are).
  el.btnOverlayExport.addEventListener("click", () => {
    playOverlayFromCache(Number(el.exportFps.value) || 8);
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
