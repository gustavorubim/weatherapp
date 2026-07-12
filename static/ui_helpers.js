/* Small, dependency-free UI helpers shared by the RadarVault shell.
 * The application controller owns radar data and selection; this module only
 * manages presentation state that is useful before that controller is wired.
 */
(() => {
  const legend = document.querySelector(".map-legend");
  const legendToggle = document.getElementById("map-legend-toggle");
  const legendContent = document.getElementById("map-legend-content");

  function setLegendExpanded(expanded) {
    if (!legend || !legendToggle || !legendContent) return;
    const isExpanded = Boolean(expanded);
    legendToggle.setAttribute("aria-expanded", String(isExpanded));
    legendContent.hidden = !isExpanded;
    legend.classList.toggle("is-collapsed", !isExpanded);
  }

  if (legendToggle) {
    legendToggle.addEventListener("click", () => {
      setLegendExpanded(legendToggle.getAttribute("aria-expanded") !== "true");
    });
  }

  // Keep the full legend visible on desktop and collapsed by default on phones.
  const mobileQuery = window.matchMedia("(max-width: 900px)");
  const syncLegendForViewport = (event) => setLegendExpanded(!event.matches);
  syncLegendForViewport(mobileQuery);
  if (typeof mobileQuery.addEventListener === "function") {
    mobileQuery.addEventListener("change", syncLegendForViewport);
  } else if (typeof mobileQuery.addListener === "function") {
    mobileQuery.addListener(syncLegendForViewport);
  }

  window.RadarVaultUI = Object.freeze({
    setLegendExpanded,
    setFilterCount(count, total) {
      const node = document.getElementById("radar-filter-count");
      if (!node) return;
      if (Number.isFinite(total) && total >= 0) {
        node.textContent = `${count} of ${total} radars shown`;
      } else {
        node.textContent = count == null ? "" : `${count} radars shown`;
      }
    },
  });
})();
