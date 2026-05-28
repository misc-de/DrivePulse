"""Map traffic layer mixin — Autobahn API, filtering, popover."""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from gi.repository import GLib, Gtk

from drivepulse_app.common import _translate
from drivepulse_app.diagnostics import get_logger
from drivepulse_app.map._jsbridge import js_call
from drivepulse_app.map.services import bab_fetch_sources

log = get_logger(__name__)


class MapTrafficMixin:
    """Traffic layer — Bundesautobahn API fetch, filtering and detail widgets."""

    # Concrete MapPage state surfaced to this mixin. See project_mixin_typing.md.
    language: str
    _backend: str
    _route_coords: list[list[float]]
    _status_lbl: Any
    _traffic_btn: Any
    _traffic_loaded: bool
    _traffic_bundesweit: bool
    _traffic_nrw: bool
    _on_traffic_visible_changed: Callable[[bool], None] | None
    _js: Callable[[str], None]
    _shumate_set_traffic_visible: Callable[[bool], None]
    _shumate_show_traffic: Callable[..., None]

    def _on_traffic_toggled(self, btn: Gtk.ToggleButton) -> None:
        visible = btn.get_active()
        self._traffic_visible = visible
        if self._backend == "webkit":
            self._js(js_call("mapSetTrafficVisible", visible))
        else:
            self._shumate_set_traffic_visible(visible)
        if visible and not self._traffic_loaded:
            self._traffic_loaded = True
            self._status_lbl.set_text(_translate(self.language, "map.traffic.loading"))
            threading.Thread(target=self._load_traffic_thread, daemon=True).start()
        if self._on_traffic_visible_changed is not None:
            self._on_traffic_visible_changed(visible)

    def set_traffic_sources(self, *, bundesweit: bool, nrw: bool) -> None:
        """Update data-source flags; resets cached state so next toggle re-fetches."""
        if bundesweit != self._traffic_bundesweit or nrw != self._traffic_nrw:
            self._traffic_bundesweit = bundesweit
            self._traffic_nrw = nrw
            self._traffic_loaded = False

    def _load_traffic_thread(self) -> None:
        items = bab_fetch_sources(
            bundesweit=self._traffic_bundesweit,
            nrw=self._traffic_nrw,
        )
        GLib.idle_add(self._show_traffic, items)

    def _parse_traffic_items(self, items: list[dict]) -> list[dict]:
        result: list[dict] = []
        for item in items[:500]:
            point = item.get("point") or ""
            try:
                parts = point.split(",")
                lat = float(parts[0].strip())
                lon = float(parts[1].strip())
            except (ValueError, IndexError):
                continue
            if lat == 0.0 and lon == 0.0:
                continue
            kind = item.get("_kind", "incidents")
            desc_raw = item.get("description") or []
            if isinstance(desc_raw, str):
                description = [desc_raw]
            else:
                description = [str(s) for s in desc_raw if s]
            title = item.get("title") or (description[0] if description else kind)
            subtitle = item.get("subtitle") or ""
            road = item.get("_road", "")
            start_ts = item.get("startTimestamp") or ""
            is_blocked_raw = item.get("isBlocked")
            if isinstance(is_blocked_raw, bool):
                is_blocked = is_blocked_raw
            else:
                is_blocked = str(is_blocked_raw or "").lower() == "true"
            delay = item.get("delayTimeValue") or ""
            result.append({
                "lat": lat,
                "lon": lon,
                "kind": kind,
                "title": title,
                "subtitle": subtitle,
                "description": description,
                "road": road,
                "start": start_ts,
                "blocked": is_blocked,
                "delay": str(delay),
            })
        return result

    def _show_traffic(self, items: list[dict]) -> bool:
        parsed = self._parse_traffic_items(items)

        if self._backend == "webkit":
            # WebKit filters by route bounding box inside JS (mapSetTraffic).
            self._js(js_call("mapSetTraffic", parsed))
            if self._traffic_btn is not None and self._traffic_btn.get_active():
                self._js("mapSetTrafficVisible(true)")
        else:
            filtered = self._filter_traffic_by_route(parsed)
            self._shumate_show_traffic(filtered)

        if self._traffic_btn is not None and self._traffic_btn.get_active():
            self._status_lbl.set_text(
                _translate(self.language, "map.traffic.count").format(count=len(parsed))
            )
        return False

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

        return box
