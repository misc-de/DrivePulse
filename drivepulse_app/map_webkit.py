"""WebKit backend helpers for the map page."""
from __future__ import annotations

import json
import os
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from .diagnostics import get_logger


log = get_logger(__name__)

HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "map.html")

WEBKIT_OK = False
WebKit: Any = None
try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit as WebKit  # type: ignore[attr-defined,no-redef]
    WEBKIT_OK = True
except (ValueError, ImportError):
    try:
        gi.require_version("WebKit2", "4.1")
        from gi.repository import WebKit2 as WebKit  # type: ignore[attr-defined,no-redef]
        WEBKIT_OK = True
    except (ValueError, ImportError):
        pass


class MapWebKitMixin:
    _webview: Any
    _backend: str

    def _setup_webview(self) -> Gtk.Widget:
        self._webview = WebKit.WebView()
        self._webview.set_hexpand(True)
        self._webview.set_vexpand(True)

        settings = self._webview.get_settings()
        for prop, val in (
            ("allow-file-access-from-file-urls", True),
            ("allow-universal-access-from-file-urls", True),
            ("enable-accelerated-2d-canvas", True),
            ("enable-webgl", True),
        ):
            try:
                settings.set_property(prop, val)
            except Exception:
                pass

        ucm = self._webview.get_user_content_manager()
        try:
            ucm.register_script_message_handler("drivepulse")
        except TypeError:
            try:
                ucm.register_script_message_handler("drivepulse", None)
            except Exception:
                pass
        ucm.connect("script-message-received::drivepulse", self._on_js_message)
        self._webview.connect("load-changed", self._on_webview_load_changed)

        try:
            with open(HTML_PATH, encoding="utf-8") as fh:
                html = fh.read()
            self._webview.load_html(html, "file:///")
        except OSError as exc:
            log.error("Could not load map.html: %s", exc)

        return self._webview

    def _on_webview_load_changed(self, _wv: Any, load_event: Any) -> None:
        # LoadEvent.FINISHED == 3 in both WebKit 6 and WebKit2
        if int(load_event) == 3:
            GLib.timeout_add(150, self._do_map_resize)

    def _js(self, code: str) -> None:
        if self._webview is None:
            return
        try:
            if hasattr(self._webview, "evaluate_javascript"):
                self._webview.evaluate_javascript(code, -1, None, None, None, None, None)
            else:
                self._webview.run_javascript(code, None, None, None)
        except Exception as exc:
            log.debug("JS call failed: %s", exc)

    def _do_map_resize(self) -> bool:
        self._js("mapResize()")
        if self._webview is not None:
            self._webview.queue_draw()
        return False

    def on_shown(self) -> None:
        """Call when the map tab becomes visible so MapLibre can measure canvas size."""
        if self._backend != "webkit":
            return
        self._do_map_resize()
        GLib.timeout_add(200, self._do_map_resize)

    def _on_js_message(self, _ucm: Any, *args: Any) -> None:
        try:
            msg = args[-1]
            js_val = msg.get_js_value()
            data = json.loads(js_val.to_json(0))
            if data.get("action") == "follow_off":
                GLib.idle_add(self._set_follow, False)
        except Exception as exc:
            log.debug("JS message error: %s", exc)
