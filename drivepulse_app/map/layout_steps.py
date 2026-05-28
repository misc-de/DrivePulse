"""Turn-by-turn steps panel — list of maneuvers with active-step highlight + auto-scroll."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from gi.repository import GLib, Gtk

from drivepulse_app.common import _translate
from drivepulse_app.map._jsbridge import js_call
from drivepulse_app.map.services import (
    format_distance,
    maneuver_icon,
    maneuver_text_key,
)


class MapStepsPanelMixin:
    """Side panel listing all turn-by-turn steps of the active tour."""

    # Concrete MapPage state surfaced to this mixin. See project_mixin_typing.md.
    language: str
    units: str
    _backend: str
    _tour_active: bool
    _tour_paused: bool
    _tour_steps: list[dict]
    _tour_step_idx: int
    _js: Callable[[str], None]
    _map_set_replay_marker: Callable[..., Any]
    _map_clear_replay_marker: Callable[..., Any]

    def _build_steps_panel(self) -> Gtk.Widget:
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        wrap.add_css_class("dp-steps-panel")
        wrap.set_halign(Gtk.Align.START)
        wrap.set_valign(Gtk.Align.FILL)
        wrap.set_margin_start(12)
        # Sit below the stacked tour-controls column
        # (start button + info button + spacing + top margin ≈ 105 px).
        wrap.set_margin_top(105)
        # Shumate: end at the TTS button's bottom edge (FAB margin_bottom=36)
        # so the panel doesn't overlap the scale ruler that shares this corner.
        wrap.set_margin_bottom(36 if self._backend == "shumate" else 12)
        wrap.set_size_request(280, -1)
        wrap.set_visible(False)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("navigation-sidebar")
        listbox.connect("row-activated", self._on_step_row_activated)
        scrolled.set_child(listbox)
        wrap.append(scrolled)

        self._steps_panel = wrap
        self._steps_listbox = listbox
        self._steps_scrolled = scrolled
        return wrap

    def _on_steps_toggle(self, btn: Gtk.ToggleButton) -> None:
        if self._steps_panel is None:
            return
        show = btn.get_active() and bool(self._tour_steps)
        if show:
            self._rebuild_steps_list()
        self._set_steps_panel_visible(show)
        if show:
            self._scroll_steps_to_active()

    def _set_steps_panel_visible(self, visible: bool) -> None:
        """Toggle the steps panel + hide the shumate scale while it's open."""
        if self._steps_panel is not None:
            self._steps_panel.set_visible(visible)
        if self._backend == "shumate" and hasattr(self, "_shumate_set_scale_visible"):
            self._shumate_set_scale_visible(not visible)
        if hasattr(self, "_update_left_chrome_visibility"):
            self._update_left_chrome_visibility()

    def _on_step_row_activated(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        lat = getattr(row, "_step_lat", None)
        lon = getattr(row, "_step_lon", None)
        if lat is None or lon is None:
            return
        prev = getattr(self, "_step_preview_row", None)
        if prev is row:
            self._map_clear_replay_marker()
            return
        self._step_preview_row = row
        self._map_set_replay_marker(lat, lon)
        if self._backend == "shumate":
            smap = getattr(self, "_shumate_map", None)
            if smap is not None:
                smap.get_viewport().set_location(lat, lon)
        elif self._backend == "webkit":
            self._js(js_call("mapGoTo", lat, lon, 15))

    def _rebuild_steps_list(self) -> None:
        if self._steps_listbox is None:
            return
        child = self._steps_listbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._steps_listbox.remove(child)
            child = nxt
        self._steps_row_widgets = []
        self._steps_row_listbox_rows = []

        for step in self._tour_steps:
            m_type = step.get("type", "")
            m_modifier = step.get("modifier", "")
            name = step.get("name", "") or ""
            icon_name = maneuver_icon(m_type, m_modifier)
            text = _translate(self.language, maneuver_text_key(m_type, m_modifier))
            if name and m_type not in {"arrive", "depart"}:
                text += _translate(self.language, "map.maneuver.on_street").format(name=name)

            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row_box.add_css_class("dp-steps-row")

            icon = Gtk.Image.new_from_icon_name(icon_name)
            icon.set_pixel_size(28)
            row_box.append(icon)

            text_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            text_col.set_hexpand(True)
            instr_lbl = Gtk.Label(label=text, xalign=0.0)
            instr_lbl.add_css_class("dp-steps-instr")
            instr_lbl.set_wrap(True)
            instr_lbl.set_max_width_chars(24)
            text_col.append(instr_lbl)
            dist_m = float(step.get("distance") or 0.0)
            if dist_m > 0:
                dist_lbl = Gtk.Label(label=format_distance(dist_m, self.units), xalign=0.0)
                dist_lbl.add_css_class("dp-steps-distance")
                dist_lbl.add_css_class("dim-label")
                text_col.append(dist_lbl)
            row_box.append(text_col)

            row = Gtk.ListBoxRow()
            row.set_activatable(True)
            row.set_selectable(False)
            row.set_child(row_box)
            row._step_lat = float(step.get("lat") or 0.0)
            row._step_lon = float(step.get("lon") or 0.0)
            self._steps_listbox.append(row)
            self._steps_row_widgets.append(row_box)
            self._steps_row_listbox_rows.append(row)

        self._highlight_active_step()

    def _highlight_active_step(self) -> None:
        active = self._tour_step_idx if (self._tour_active or self._tour_paused) else -1
        for idx, row_box in enumerate(self._steps_row_widgets):
            row_box.remove_css_class("dp-steps-row-active")
            row_box.remove_css_class("dp-steps-row-done")
            if active < 0:
                continue
            if idx < active:
                row_box.add_css_class("dp-steps-row-done")
            elif idx == active:
                row_box.add_css_class("dp-steps-row-active")
        if active >= 0:
            self._scroll_steps_to_active()

    def _scroll_steps_to_active(self) -> None:
        """Auto-scroll the steps panel so the active step is near the top
        — gives the driver a preview of upcoming maneuvers below it."""
        if (
            self._steps_scrolled is None
            or self._steps_panel is None
            or not self._steps_panel.get_visible()
            or not self._steps_row_listbox_rows
        ):
            return
        idx = self._tour_step_idx
        if idx < 0 or idx >= len(self._steps_row_listbox_rows):
            return
        # Defer until after GTK has allocated the rows, otherwise heights are 0.
        GLib.idle_add(self._do_scroll_to_active, idx)

    def _do_scroll_to_active(self, idx: int) -> bool:
        if self._steps_scrolled is None or idx >= len(self._steps_row_listbox_rows):
            return False
        adj = self._steps_scrolled.get_vadjustment()
        if adj is None:
            return False
        upper = adj.get_upper()
        page = adj.get_page_size()
        if upper <= page:
            return False  # all rows already fit
        n = max(1, len(self._steps_row_listbox_rows))
        row_h = upper / n
        # Pin the active step ~one row down from the top of the viewport.
        target = max(0.0, min(upper - page, idx * row_h - row_h))
        adj.set_value(target)
        return False
