"""Map-state polling: zoom/pitch/bearing readout for the (now hidden) mock
debug overlay. Lives as a separate mixin because the WebKit path uses
evaluate_javascript with a finish callback that needs to stay together."""
from __future__ import annotations

import json
from typing import Any

from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)


class MapStatePollMixin:
    def _ensure_map_state_poll(self) -> None:
        # Map-state polling is intentionally disabled: the bottom-left
        # backend/zoom/pitch/bearing readout is hidden in mock mode now,
        # so there's nothing to feed.
        return

    def _poll_map_state(self) -> bool:
        if not self.mock_mode:
            self._map_state_poll_id = None
            return False
        # Shumate: read directly from the viewport (2D, no pitch/bearing).
        if self._backend == "shumate" and self._shumate_map is not None:
            try:
                viewport = self._shumate_map.get_viewport()
                self._map_zoom = float(viewport.get_zoom_level())
                self._map_pitch = None
                self._map_bearing = None
            except Exception:
                log.debug("Could not read Shumate viewport zoom", exc_info=True)
        # WebKit: query via evaluate_javascript — the script-message-handler
        # bridge proved unreliable in our deployment, so we just RPC the values
        # out directly. The callback updates the cached fields.
        elif self._backend == "webkit" and self._webview is not None:
            self._evaluate_webkit_state()
        self._refresh_map_state_status()
        return True

    def _evaluate_webkit_state(self) -> None:
        script = (
            "(function(){try{if(typeof map==='undefined'||!map)return null;"
            "return JSON.stringify([map.getZoom(),map.getPitch(),map.getBearing()]);"
            "}catch(e){return null;}})()"
        )
        try:
            if hasattr(self._webview, "evaluate_javascript"):
                # WebKit 6: 7 args incl. callback
                self._webview.evaluate_javascript(
                    script, -1, None, None, None,
                    self._on_webkit_state_eval, None,
                )
            else:
                # WebKit2: run_javascript(script, cancellable, callback, user_data)
                self._webview.run_javascript(
                    script, None, self._on_webkit_state_eval, None,
                )
        except Exception:
            log.debug("evaluate_javascript failed", exc_info=True)

    def _on_webkit_state_eval(self, webview: Any, result: Any, _user: Any) -> None:
        try:
            if hasattr(webview, "evaluate_javascript_finish"):
                js_val = webview.evaluate_javascript_finish(result)
            else:
                js_val = webview.run_javascript_finish(result).get_js_value()
            raw = js_val.to_string() if js_val is not None else None
            if not raw or raw == "null":
                return
            try:
                z, p, b = json.loads(raw)
            except (ValueError, TypeError, json.JSONDecodeError):
                log.debug("WebKit returned unparseable map-state: %r", raw, exc_info=True)
                return
            self._map_zoom = float(z)
            self._map_pitch = float(p)
            self._map_bearing = float(b)
            self._refresh_map_state_status()
        except Exception:
            log.debug("evaluate_javascript_finish failed", exc_info=True)

    def _refresh_map_state_status(self) -> None:
        """No-op: the mock-mode bottom-left readout (backend/zoom/pitch/bearing)
        used to overlay debug numbers on the map. It's intentionally hidden
        now so the demo view stays clean."""
        if self._map_state_overlay is not None:
            self._map_state_overlay.set_visible(False)

    def _on_js_map_state(self, zoom: float, pitch: float, bearing: float) -> None:
        self._map_zoom = zoom
        self._map_pitch = pitch
        self._map_bearing = bearing
        self._refresh_map_state_status()
