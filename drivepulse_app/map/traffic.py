"""Map traffic layer mixin — Autobahn + city feeds, refresh, popover."""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from gi.repository import GLib, Gtk

from drivepulse_app.common import _translate
from drivepulse_app.diagnostics import get_logger
from drivepulse_app.map._jsbridge import js_call
from drivepulse_app.map.services import fetch_traffic

log = get_logger(__name__)

# City congestion feeds update every 5 min — refresh at the same pace while
# the layer is visible.
TRAFFIC_REFRESH_S = 300


class MapTrafficMixin:
    """Traffic layer — fetches all enabled sources, keeps them fresh, renders details."""

    # Concrete MapPage state surfaced to this mixin. See project_mixin_typing.md.
    language: str
    _backend: str
    _route_coords: list[list[float]]
    _status_lbl: Any
    _traffic_btn: Any
    _traffic_visible: bool
    _traffic_loaded: bool
    _traffic_fetching: bool
    _traffic_refresh_id: int
    _traffic_bundesweit: bool
    _traffic_nrw: bool
    _traffic_city: bool
    _on_traffic_visible_changed: Callable[[bool], None] | None
    _js: Callable[[str], None]
    _shumate_set_traffic_visible: Callable[[bool], None]
    _shumate_show_traffic: Callable[..., None]
    _shumate_show_traffic_flow: Callable[..., None]

    def _on_traffic_toggled(self, btn: Gtk.ToggleButton) -> None:
        visible = btn.get_active()
        self._traffic_visible = visible
        if self._backend == "webkit":
            self._js(js_call("mapSetTrafficVisible", visible))
        else:
            self._shumate_set_traffic_visible(visible)
        if visible:
            self._ensure_traffic_loaded()
        else:
            self._stop_traffic_refresh()
        if self._on_traffic_visible_changed is not None:
            self._on_traffic_visible_changed(visible)

    def set_traffic_sources(self, *, bundesweit: bool, nrw: bool, city: bool) -> None:
        """Update data-source flags; reloads right away when the layer is shown."""
        if (bundesweit, nrw, city) == (
            self._traffic_bundesweit, self._traffic_nrw, self._traffic_city,
        ):
            return
        self._traffic_bundesweit = bundesweit
        self._traffic_nrw = nrw
        self._traffic_city = city
        self._traffic_loaded = False
        if self._traffic_visible:
            self._ensure_traffic_loaded()

    def _ensure_traffic_loaded(self) -> None:
        """First load (with status text) plus the periodic refresh timer."""
        if not self._traffic_loaded:
            self._traffic_loaded = True
            self._status_lbl.set_text(_translate(self.language, "map.traffic.loading"))
            self._request_traffic_load(announce=True)
        if not self._traffic_refresh_id:
            self._traffic_refresh_id = GLib.timeout_add_seconds(
                TRAFFIC_REFRESH_S, self._on_traffic_refresh_tick,
            )

    def _stop_traffic_refresh(self) -> None:
        if self._traffic_refresh_id:
            GLib.source_remove(self._traffic_refresh_id)
            self._traffic_refresh_id = 0

    def _on_traffic_refresh_tick(self) -> bool:
        # The dashboard drops an idle MapPage by unparenting it; without this
        # check the timer would keep the dead page alive and fetching.
        detached = getattr(self, "get_root", lambda: None)() is None
        if not self._traffic_visible or detached:
            self._traffic_refresh_id = 0
            return False
        if getattr(self, "get_mapped", lambda: True)():
            self._request_traffic_load(announce=False)
        return True

    def _request_traffic_load(self, *, announce: bool) -> None:
        if self._traffic_fetching:
            return
        self._traffic_fetching = True
        threading.Thread(
            target=self._load_traffic_thread, args=(announce,), daemon=True,
        ).start()

    def _load_traffic_thread(self, announce: bool = True) -> None:
        try:
            data = fetch_traffic(
                bundesweit=self._traffic_bundesweit,
                nrw=self._traffic_nrw,
                city=self._traffic_city,
            )
        except Exception:
            log.warning("Traffic fetch failed", exc_info=True)
            data = {"events": [], "flow": []}
        GLib.idle_add(self._show_traffic, data, announce)

    def _show_traffic(self, data: dict[str, list[dict]], announce: bool = True) -> bool:
        self._traffic_fetching = False
        events = data.get("events") or []
        flow = data.get("flow") or []
        if not announce and not events and not flow:
            # A refresh that came back empty is almost always a network
            # hiccup — keep showing the previous data instead of wiping it.
            return False

        if self._backend == "webkit":
            # WebKit filters events by route bounding box inside JS (mapSetTraffic).
            self._js(js_call("mapSetTraffic", events))
            self._js(js_call("mapSetTrafficFlow", flow))
            self._js(js_call("mapSetTrafficVisible", self._traffic_visible))
        else:
            self._shumate_show_traffic(self._filter_traffic_by_route(events))
            self._shumate_show_traffic_flow(flow)

        # Periodic refreshes stay silent — the status line is shared with
        # routing/tour messages that shouldn't be clobbered every 5 min.
        if announce and self._traffic_visible:
            self._status_lbl.set_text(self._traffic_summary(events, flow))
        return False

    def _traffic_summary(self, events: list[dict], flow: list[dict]) -> str:
        jams = sum(1 for seg in flow if seg.get("level") == "jam")
        slow = sum(1 for seg in flow if seg.get("level") == "slow")
        text = _translate(self.language, "map.traffic.count").format(count=len(events))
        if flow:
            text += " · " + _translate(self.language, "map.traffic.flow_summary").format(
                jam=jams, slow=slow,
            )
        return text

    def _filter_traffic_by_route(self, items: list[dict]) -> list[dict]:
        """Keep only items within ~5 km of the route bounding box.

        When no route is loaded yet, return everything — otherwise the user
        toggles traffic on, sees nothing, and assumes the feature is broken.
        """
        if not self._route_coords:
            return list(items)
        lats = [c[1] for c in self._route_coords]
        lons = [c[0] for c in self._route_coords]
        pad = 0.05  # ~5 km
        min_lat, max_lat = min(lats) - pad, max(lats) + pad
        min_lon, max_lon = min(lons) - pad, max(lons) + pad
        return [
            item for item in items
            if min_lat <= item["lat"] <= max_lat and min_lon <= item["lon"] <= max_lon
        ]

    def _format_traffic_timestamp(self, raw: str) -> str:
        """Render API ISO timestamps as a short local-time string."""
        if not raw:
            return ""
        try:
            from datetime import datetime
            cleaned = raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(cleaned)
            if dt.tzinfo is not None:
                dt = dt.astimezone()
            return dt.strftime("%d.%m.%Y %H:%M")
        except (ValueError, TypeError):
            return raw

    def _build_traffic_detail_widget(self, item: dict) -> Gtk.Widget:
        """Build the popover/popup content shown for one traffic event."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_size_request(280, -1)

        road = item.get("road") or ""
        title = item.get("title") or ""
        header_text = f"{road} — {title}" if road and title else (road or title)
        if header_text:
            header = Gtk.Label(label=header_text, xalign=0.0)
            header.add_css_class("title-4")
            header.set_wrap(True)
            header.set_max_width_chars(36)
            box.append(header)

        subtitle = item.get("subtitle") or ""
        if subtitle:
            sub = Gtk.Label(label=subtitle, xalign=0.0)
            sub.add_css_class("dim-label")
            sub.set_wrap(True)
            sub.set_max_width_chars(40)
            box.append(sub)

        if item.get("blocked"):
            blocked = Gtk.Label(
                label=_translate(self.language, "map.traffic.blocked"),
                xalign=0.0,
            )
            blocked.add_css_class("error")
            box.append(blocked)

        delay = item.get("delay") or ""
        if delay and delay != "0":
            delay_lbl = Gtk.Label(
                label=_translate(self.language, "map.traffic.delay").format(min=delay),
                xalign=0.0,
            )
            box.append(delay_lbl)

        start = self._format_traffic_timestamp(item.get("start") or "")
        if start:
            start_lbl = Gtk.Label(
                label=_translate(self.language, "map.traffic.since").format(time=start),
                xalign=0.0,
            )
            start_lbl.add_css_class("dim-label")
            box.append(start_lbl)

        description = item.get("description") or []
        if description:
            sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep.set_margin_top(2)
            sep.set_margin_bottom(2)
            box.append(sep)
            desc_text = "\n".join(description)
            desc = Gtk.Label(label=desc_text, xalign=0.0)
            desc.set_wrap(True)
            desc.set_max_width_chars(40)
            box.append(desc)

        source = item.get("source") or ""
        if source:
            src_lbl = Gtk.Label(
                label=_translate(self.language, "map.traffic.source").format(source=source),
                xalign=0.0,
            )
            src_lbl.add_css_class("dim-label")
            src_lbl.add_css_class("caption")
            box.append(src_lbl)

        return box
