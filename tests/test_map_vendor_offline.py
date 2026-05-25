"""Guard: the WebKit map must boot without any network round-trip.

Regression. Until commit <fixme>, map.html referenced MapLibre via
``https://unpkg.com/...``. The synchronous load_html() call on the main
GTK thread therefore blocked on DNS/connect when offline, freezing the
entire app for the system DNS timeout. The fix bundles MapLibre under
drivepulse_app/map/vendor/. These tests assert the contract so a future
edit that re-introduces an external CDN reference fails CI.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

MAP_DIR = Path(__file__).resolve().parent.parent / "drivepulse_app" / "map"
MAP_HTML = MAP_DIR / "map.html"
VENDOR_DIR = MAP_DIR / "vendor"


def test_map_html_uses_relative_maplibre_paths():
    html = MAP_HTML.read_text(encoding="utf-8")
    # No external CDN references for the renderer/CSS — only tile sources
    # (which are intentionally lazy and only hit when MapLibre fetches them).
    assert "unpkg.com/maplibre" not in html, (
        "map.html must not load MapLibre from a CDN — keeps startup offline-safe"
    )
    # Two relative references must be present: CSS and JS.
    rel_refs = re.findall(r'(?:href|src)="(vendor/[^"]+)"', html)
    assert any(p.endswith(".css") for p in rel_refs), "map.html must link the vendored maplibre-gl.css"
    assert any(p.endswith(".js") for p in rel_refs), "map.html must script the vendored maplibre-gl.js"


def test_vendored_maplibre_files_are_present_and_nontrivial():
    """The vendor dir must hold both maplibre-gl.js and maplibre-gl.css,
    each at least a few KB — guards against an accidental empty-file
    commit that would silently break the map at runtime."""
    matches = list(VENDOR_DIR.glob("maplibre-gl-*/maplibre-gl.js"))
    assert matches, f"no vendored maplibre-gl.js under {VENDOR_DIR}"
    js_path = matches[0]
    css_path = js_path.with_name("maplibre-gl.css")
    assert css_path.exists(), f"missing {css_path}"
    # MapLibre is ~800 KB minified, CSS ~60 KB. Sanity floors.
    assert js_path.stat().st_size > 200_000, "vendored maplibre-gl.js looks truncated"
    assert css_path.stat().st_size > 10_000, "vendored maplibre-gl.css looks truncated"


def test_map_html_references_existing_vendor_paths():
    """Each `vendor/...` path written into map.html must resolve to a
    real file on disk relative to the HTML's directory — same way WebKit
    resolves them at runtime with the file:// base URI."""
    html = MAP_HTML.read_text(encoding="utf-8")
    for rel in re.findall(r'(?:href|src)="(vendor/[^"]+)"', html):
        resolved = MAP_DIR / rel
        assert resolved.is_file(), f"map.html references missing vendor file: {rel}"


@pytest.mark.parametrize("setting", ["packages.find", "package-data"])
def test_pyproject_keeps_vendor_in_the_wheel(setting):
    """Without the package-data declaration the vendored JS/CSS would be
    stripped from the installed wheel and the WebKit map would fall back
    to the CDN — defeating the offline-startup fix at install time."""
    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    if setting == "packages.find":
        assert 'include = ["drivepulse_app*"' in pyproject
    else:
        assert '"drivepulse_app.map"' in pyproject
        assert "vendor/**/*" in pyproject
