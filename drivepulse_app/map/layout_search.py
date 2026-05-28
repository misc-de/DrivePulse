"""Map page route-search bar — waypoint entries with drag-and-drop reordering."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from gi.repository import Gdk, GObject, Gtk

from drivepulse_app.common import _translate


class MapSearchBarMixin:
    """Multi-waypoint route entry rows: start + end + optional intermediate stops."""

    # Declared here so mypy knows the optional-int type before the mixin clears
    # the attribute on user actions. Owning class (MapPage) initialises the
    # concrete value in __init__.
    _loaded_tour_id: int | None

    # Concrete MapPage state surfaced to this mixin. See project_mixin_typing.md.
    language: str
    _entry_rows: list[tuple[Gtk.Box, Gtk.Entry, Gtk.Widget]]
    _map_content_box: Gtk.Box
    _on_route_clicked: Callable[..., Any]

    def _build_search_bar(self) -> None:
        bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._search_bar = bar
        bar.set_margin_top(8)
        bar.set_margin_bottom(4)
        bar.set_margin_start(8)
        bar.set_margin_end(8)

        self._entries_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        bar.append(self._entries_container)

        # Two initial rows: start + end
        self._entry_rows = []
        for _ in range(2):
            self._entries_container.append(self._make_entry_row())
        self._update_placeholders()
        self._update_remove_sensitivity()

        # Action row: [route-btn] [status]
        action = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        self._route_btn = Gtk.Button()
        self._route_btn.set_label(_translate(self.language, "map.route"))
        self._route_btn.add_css_class("suggested-action")
        self._route_btn.connect("clicked", self._on_route_clicked)
        # Spinner that takes the button's place while the route is computing.
        self._route_btn_spinner = Gtk.Spinner()
        self._route_btn_spinner.set_size_request(20, 20)

        self._status_lbl = Gtk.Label(label="")
        self._status_lbl.add_css_class("dim-label")
        self._status_lbl.set_hexpand(True)
        self._status_lbl.set_halign(Gtk.Align.START)

        for w in (self._status_lbl, self._route_btn):
            action.append(w)
        bar.append(action)
        bar.set_visible(False)
        self._map_content_box.append(bar)

    def _make_entry_row(self) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)

        # Drag handle
        handle = Gtk.Image.new_from_icon_name("list-drag-handle-symbolic")
        handle.add_css_class("dim-label")
        handle.set_cursor(Gdk.Cursor.new_from_name("grab"))
        handle.set_margin_start(2)
        handle.set_margin_end(2)

        entry = Gtk.Entry()
        entry.set_hexpand(True)
        entry.connect("activate", self._on_route_clicked)
        entry.connect("changed", self._on_entry_text_changed)

        add_btn = Gtk.Button(label="+")
        add_btn.add_css_class("flat")
        add_btn.set_valign(Gtk.Align.CENTER)
        add_btn.connect("clicked", lambda _b, r=row: self._insert_entry_after(r))

        rem_btn = Gtk.Button(label="−")
        rem_btn.add_css_class("flat")
        rem_btn.set_valign(Gtk.Align.CENTER)
        rem_btn.connect("clicked", lambda _b, r=row: self._remove_entry(r))

        row.append(handle)
        row.append(entry)
        row.append(add_btn)
        row.append(rem_btn)

        # DnD: drag source on handle
        drag_src = Gtk.DragSource.new()
        drag_src.set_actions(Gdk.DragAction.MOVE)
        drag_src.connect("prepare", lambda src, x, y, r=row: self._drag_prepare(src, x, y, r))
        handle.add_controller(drag_src)

        # DnD: drop target on the whole row
        drop_tgt = Gtk.DropTarget.new(GObject.TYPE_INT, Gdk.DragAction.MOVE)
        drop_tgt.connect("drop", lambda tgt, val, x, y, r=row: self._drag_drop(tgt, val, x, y, r))
        drop_tgt.connect("motion", lambda tgt, x, y: Gdk.DragAction.MOVE)
        row.add_controller(drop_tgt)

        self._entry_rows.append((row, entry, rem_btn))
        return row

    def _drag_prepare(
        self, _src: Gtk.DragSource, _x: float, _y: float, row: Gtk.Box
    ) -> Gdk.ContentProvider | None:
        idx = next((i for i, (r, _, __) in enumerate(self._entry_rows) if r is row), -1)
        if idx < 0:
            return None
        self._dnd_src_idx = idx
        gval = GObject.Value()
        gval.init(GObject.TYPE_INT)
        gval.set_int(idx)
        return Gdk.ContentProvider.new_for_value(gval)

    def _drag_drop(
        self, _tgt: Gtk.DropTarget, _val: object, _x: float, _y: float, dst_row: Gtk.Box
    ) -> bool:
        src_idx = self._dnd_src_idx
        dst_idx = next(
            (i for i, (r, _, __) in enumerate(self._entry_rows) if r is dst_row), -1
        )
        if src_idx < 0 or dst_idx < 0 or src_idx == dst_idx:
            return False
        self._reorder_row(src_idx, dst_idx)
        return True

    def _reorder_row(self, src_idx: int, dst_idx: int) -> None:
        if getattr(self, "_loaded_tour_id", None) is not None:
            self._loaded_tour_id = None
        triple = self._entry_rows.pop(src_idx)
        self._entry_rows.insert(dst_idx, triple)
        row_widget = triple[0]
        if self._entries_container is not None:
            self._entries_container.remove(row_widget)
            if dst_idx == 0:
                self._entries_container.prepend(row_widget)
            else:
                prev_sibling = self._entry_rows[dst_idx - 1][0]
                self._entries_container.insert_child_after(row_widget, prev_sibling)
        self._update_placeholders()

    def _insert_entry_after(self, after_row: Gtk.Box) -> None:
        idx = next(i for i, (r, _, __) in enumerate(self._entry_rows) if r is after_row)
        new_row = self._make_entry_row()
        # _make_entry_row appended to list; move it to correct position
        triple = self._entry_rows.pop()
        self._entry_rows.insert(idx + 1, triple)
        if self._entries_container is not None:
            self._entries_container.insert_child_after(new_row, after_row)
        self._update_placeholders()
        self._update_remove_sensitivity()
        # no grab_focus — avoids keyboard popup on mobile

    def _remove_entry(self, row: Gtk.Box) -> None:
        idx = next(i for i, (r, _, __) in enumerate(self._entry_rows) if r is row)
        if len(self._entry_rows) <= 2:
            self._entry_rows[idx][1].set_text("")
            return
        self._entry_rows.pop(idx)
        if self._entries_container is not None:
            self._entries_container.remove(row)
        self._update_placeholders()
        self._update_remove_sensitivity()

    def _update_placeholders(self) -> None:
        n = len(self._entry_rows)
        for i, (_, entry, __) in enumerate(self._entry_rows):
            if i == 0:
                key = "map.search.start"
            elif i == n - 1:
                key = "map.search.end"
            else:
                key = "map.search.waypoint"
            entry.set_placeholder_text(_translate(self.language, key))

    def _update_remove_sensitivity(self) -> None:
        for _, __, rem_btn in self._entry_rows:
            rem_btn.set_sensitive(True)

    def _on_entry_text_changed(self, _entry: object) -> None:
        if not getattr(self, "_loading_tour", False) and getattr(self, "_loaded_tour_id", None) is not None:
            self._loaded_tour_id = None
