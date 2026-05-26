"""Lazy create / auto-unload of the MapPage widget.

MapPage owns WebKit (or Shumate) plus its routing/replay state, both of which
are heavyweight. The page is only built when the user first switches to the
Map tab, and is torn down again after a few minutes of inactivity on other
tabs — unless a tour is active or paused, which holds it open.
"""
from __future__ import annotations

from gi.repository import GLib, Gtk

from drivepulse_app.map.page import MapPage


class DashboardMapLifecycleMixin:
    # Seconds of inactivity on any other tab before the map widget is destroyed.
    _MAP_IDLE_UNLOAD_S = 3 * 60

    def _ensure_map_page(self) -> None:
        """Create MapPage on first use and restore any previously saved state."""
        if self.map_page is not None:
            return
        self.map_page = MapPage(
            self.language,
            force_webkit=self.force_webkit_map,
            units=self.units,
            mock_mode=self.mock_mode,
            poi_visible=False,
            traffic_visible=self.map_traffic_visible,
            traffic_bundesweit=self.map_traffic_bundesweit,
            traffic_nrw=self.map_traffic_nrw,
            map_3d_view=self.map_3d_view,
            map_layer=self.map_layer,
            map_heading_up=self.map_heading_up,
            on_traffic_visible_changed=self._set_map_traffic_visible,
            on_3d_view_changed=self._set_map_3d_view,
            on_map_layer_changed=self._set_map_layer,
            on_heading_up_changed=self._set_map_heading_up,
            on_tour_started=self._on_tour_started,
            on_tour_stopped=self._on_tour_stopped,
            on_tour_resumed=self._on_tour_resumed,
            on_tts_enabled_changed=self._set_tts_enabled,
            on_map_tapped=lambda: self._set_nav_visible(not self._nav_visible),
            db=self.db,
            get_sync_client=self._get_active_sync_client,
            initial_zoom=self._map_suspended_zoom,
        )
        self.map_page.set_tts_enabled(self.tts_enabled)
        self.map_page.set_speed_warn_enabled(self.speed_limit_warn)
        self.map_page.set_tts_language(self.tts_language)
        self.map_page.set_tts_voice(self.tts_voice)
        self.map_page.set_tts_quality(self.tts_quality)
        if not self._map_suspended_follow:
            self.map_page._follow_gps = False
        ff = getattr(self, "form_factor", None)
        if ff and hasattr(self.map_page, "set_form_factor"):
            self.map_page.set_form_factor(ff)
        self._map_rotator.set_child(self.map_page)

    def _schedule_map_unload(self) -> None:
        self._cancel_map_unload()
        self._map_unload_timer_id = GLib.timeout_add_seconds(
            self._MAP_IDLE_UNLOAD_S, self._unload_map_page
        )

    def _cancel_map_unload(self) -> None:
        if self._map_unload_timer_id is not None:
            GLib.source_remove(self._map_unload_timer_id)
            self._map_unload_timer_id = None

    def _unload_map_page(self) -> bool:
        self._map_unload_timer_id = None
        if self.map_page is None:
            return False
        # Keep alive during active navigation or while the tab is open.
        if getattr(self.map_page, "_tour_active", False) or getattr(self.map_page, "_tour_paused", False):
            return False
        if self.view_stack.get_visible_child_name() == self.PAGE_MAP:
            return False
        self._map_suspended_zoom = getattr(self.map_page, "_map_zoom", None)
        self._map_suspended_follow = getattr(self.map_page, "_follow_gps", True)
        self._map_rotator.set_child(Gtk.Box())
        self.map_page = None
        return False
