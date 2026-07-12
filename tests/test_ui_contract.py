from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).parents[1]
HTML = (ROOT / "static/index.html").read_text()
CSS = (ROOT / "static/styles.css").read_text()
HELPERS = (ROOT / "static/ui_helpers.js").read_text()


class _IdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.tags: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        if "id" in attrs_dict:
            self.ids.append(attrs_dict["id"])
        self.tags.append((tag, attrs_dict))


def _parsed() -> _IdParser:
    parser = _IdParser()
    parser.feed(HTML)
    return parser


def test_frozen_and_enhancement_ids_are_present_once() -> None:
    frozen = {
        "selected",
        "btn-start",
        "btn-stop",
        "action-msg",
        "status-list",
        "preview-img",
        "preview-empty",
        "scrubber",
        "scrub",
        "scrub-label",
        "scrub-count",
        "btn-overlay-play",
        "btn-overlay-stop",
        "overlay-opacity",
        "overlay-hud",
        "hud-pause",
        "hud-label",
        "export-form",
        "export-start",
        "export-end",
        "export-fps",
        "btn-export",
        "export-msg",
        "export-link",
        "btn-overlay-export",
        "map",
    }
    added = {
        "radar-search",
        "radar-filter-supported",
        "radar-filter-cached",
        "status-summary",
        "status-active",
        "status-cached",
        "playback-speed",
        "playback-time-mode",
        "timezone-mode",
        "reflectivity-legend",
        "map-legend-toggle",
        "tab-archive",
        "tab-library",
        "panel-archive",
        "panel-library",
        "playback-start",
        "playback-end",
        "btn-playback-range",
        "library-summary",
        "library-list",
        "library-before",
        "btn-library-trim",
        "btn-library-clear",
        "library-msg",
    }
    ids = _parsed().ids
    assert frozen | added <= set(ids)
    assert all(ids.count(identifier) == 1 for identifier in frozen | added)


def test_hidden_and_selected_styles_are_scoped() -> None:
    assert re.search(r"\[hidden\]\s*\{[^}]*display\s*:\s*none\s*!important", CSS)
    assert ".selected-card" in CSS
    assert ".selected {" not in CSS
    assert 'class="selected-card empty"' in HTML
    assert 'class="dot selected"' not in HTML
    assert 'class="dot legend-selected"' in HTML


def test_messages_and_range_controls_have_accessible_names() -> None:
    assert 'id="action-msg"' in HTML and 'aria-live="polite"' in HTML
    assert 'id="export-msg"' in HTML and 'aria-live="polite"' in HTML
    assert 'for="scrub"' in HTML
    assert 'for="overlay-opacity"' in HTML
    assert 'aria-describedby="scrub-label scrub-count"' in HTML
    assert 'aria-valuetext="75 percent"' in HTML


def test_focus_visible_styles_cover_interactive_controls() -> None:
    assert ":focus-visible" in CSS
    assert "outline" in CSS
    assert "map-legend-toggle" in HTML
    assert 'aria-expanded="true"' in HTML


def test_search_status_and_time_control_shell_is_wired_for_later_integration() -> None:
    expected_controls = (
        'id="radar-search"',
        'id="radar-filter-supported"',
        'id="radar-filter-cached"',
        'id="status-summary"',
        'id="status-active"',
        'id="status-cached"',
        'id="playback-speed"',
        'id="playback-time-mode"',
        'id="timezone-mode"',
        'id="reflectivity-legend"',
    )
    assert all(control in HTML for control in expected_controls)
    assert "Reflectivity" in HTML and "dBZ" in HTML
    assert "@media (max-width: 900px)" in CSS
    assert "@media (max-width: 420px)" in CSS
    assert "overflow-x" not in CSS or "hidden" in CSS


def test_local_font_fallback_and_helper_are_available() -> None:
    assert "fonts.googleapis.com" not in HTML
    assert "fonts.gstatic.com" not in HTML
    assert "/static/ui_helpers.js" in HTML
    assert "window.RadarVaultUI" in HELPERS
    assert "setLegendExpanded" in HELPERS
