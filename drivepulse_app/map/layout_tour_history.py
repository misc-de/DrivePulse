"""Tour-history page on MapPage: paginated list of recorded trips and saved
tours, with multi-select for bulk delete/share, per-row rename and per-row
load (tour)/replay (trip).

The actual share dispatch lives on ``MapTourActionsMixin`` because the
header buttons and the sync-active check are shared with the saved-tour list."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from gi.repository import Adw, GLib, Gtk

from drivepulse_app.common import _translate
from drivepulse_app.diagnostics import get_logger
from drivepulse_app.map._list_helpers import make_bulk_select_header, make_empty_dim_row

log = get_logger(__name__)


class MapTourHistoryMixin:
    # Concrete MapPage state surfaced to this mixin. See project_mixin_typing.md.
    language: str
    get_root: Callable[[], Any]
    _sync_active: Callable[[], bool]
    _load_saved_tour: Callable[..., Any]
    _show_trip_replay: Callable[..., Any]
    _on_history_share_clicked: Callable[..., Any]

    # Pull this many history rows per fetch. Small enough to keep the first
    # render snappy, large enough that you don't trigger pagination on every
    # tiny scroll.
    _TOUR_HISTORY_PAGE_SIZE = 30

    def _on_tour_history_clicked(self, _btn: object) -> None:
        nav_view = getattr(self, "_nav_view", None)
        if nav_view is None:
            return

        self._tour_history_offset = 0
        self._tour_history_loading = False
        self._tour_history_exhausted = False
        self._tour_history_empty_row: Gtk.ListBoxRow | None = None
        # Selection state: set of (kind, id) tuples currently checked.
        self._tour_history_selected: set[tuple[str, int]] = set()
        # Select-mode flag: long-press on a row switches the list into
        # multi-select with checkbox prefixes; mirrors trips.py / scans.py.
        self._tour_history_select_mode: bool = False
        # All loaded row metas so we can re-render the list when toggling
        # in/out of select mode without re-querying the database.
        self._tour_history_metas: list[dict] = []

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        listbox.set_valign(Gtk.Align.START)
        self._tour_history_listbox = listbox

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        inner.set_margin_top(12)
        inner.set_margin_bottom(12)
        inner.set_margin_start(12)
        inner.set_margin_end(12)
        inner.append(listbox)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        scrolled.set_child(inner)
        scrolled.connect("edge-reached", self._on_tour_history_edge_reached)

        header, trash_btn, share_btn = make_bulk_select_header(
            trash_tooltip=_translate(self.language, "map.history.delete_selected_tooltip"),
            share_tooltip=_translate(self.language, "map.history.share_selected_tooltip"),
            on_trash=self._on_history_trash_clicked,
            on_share=self._on_history_share_clicked,
        )
        self._tour_history_trash_btn = trash_btn
        self._tour_history_share_btn = share_btn

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(header)
        toolbar_view.set_content(scrolled)

        page = Adw.NavigationPage(title=_translate(self.language, "map.topnav.history"))
        page.set_child(toolbar_view)
        nav_view.push(page)

        self._load_more_tour_history()

    def _on_tour_history_edge_reached(
        self, _sw: Gtk.ScrolledWindow, pos: Gtk.PositionType
    ) -> None:
        if pos == Gtk.PositionType.BOTTOM:
            self._load_more_tour_history()

    def _load_more_tour_history(self) -> None:
        if getattr(self, "_tour_history_loading", False):
            return
        if getattr(self, "_tour_history_exhausted", False):
            return
        db = getattr(self, "_map_db", None)
        if db is None:
            return

        self._tour_history_loading = True
        try:
            page_size = self._TOUR_HISTORY_PAGE_SIZE
            rows = db.list_tour_history(page_size, self._tour_history_offset)
            for row in rows:
                self._append_tour_history_row(row)
            if len(rows) < page_size:
                self._tour_history_exhausted = True
            self._tour_history_offset += len(rows)
            if self._tour_history_offset == 0:
                self._show_tour_history_empty()
        finally:
            self._tour_history_loading = False

    def _show_tour_history_empty(self) -> None:
        listbox = getattr(self, "_tour_history_listbox", None)
        if listbox is None or self._tour_history_empty_row is not None:
            return
        row = make_empty_dim_row(
            _translate(self.language, "map.history.empty"), margin=18,
        )
        listbox.append(row)
        self._tour_history_empty_row = row

    @staticmethod
    def _format_history_ts(ts: str) -> str:
        if not ts:
            return ""
        # ISO timestamps stored as "2026-05-23T07:32:11+00:00" — keep the
        # date+time portion, drop the seconds/timezone for the row label.
        return ts[:16].replace("T", " ")

    @staticmethod
    def _format_history_duration(seconds: float | None) -> str:
        if not seconds or seconds <= 0:
            return ""
        s = int(seconds)
        h, rem = divmod(s, 3600)
        m, _ = divmod(rem, 60)
        if h > 0:
            return f"{h}h {m:02d}min"
        return f"{m}min"

    def _format_history_title(self, row: Any) -> str:
        kind = row["kind"]
        if kind == "tour":
            return row["trip_label"] or _translate(self.language, "map.history.kind_tour")
        # trip
        car_label = row["car_label"] or row["car_brand"] or ""
        if not car_label and row["car_vin"]:
            car_label = f"VIN …{row['car_vin'][-5:]}"
        trip_label = row["trip_label"] or ""
        if car_label and trip_label:
            return f"{car_label} · {trip_label}"
        return car_label or trip_label or _translate(self.language, "map.history.kind_trip")

    def _format_history_subtitle(self, row: Any) -> str:
        ts = self._format_history_ts(row["ts"] or "")
        kind = row["kind"]
        if kind == "tour":
            return ts
        parts = [ts] if ts else []
        dist = row["distance_km"]
        if dist:
            parts.append(f"{dist:.1f} km")
        dur = self._format_history_duration(row["duration_s"])
        if dur:
            parts.append(dur)
        return "  ·  ".join(parts)

    def _append_tour_history_row(self, data: Any) -> None:
        listbox = getattr(self, "_tour_history_listbox", None)
        if listbox is None:
            return
        meta = {
            "kind": data["kind"],
            "id": int(data["id"]),
            "ts": data["ts"],
            "distance_km": data["distance_km"],
            "duration_s": data["duration_s"],
            "trip_label": data["trip_label"],
            "car_brand": data["car_brand"],
            "car_label": data["car_label"],
            "car_vin": data["car_vin"],
            "car_id": data["car_id"],
        }
        self._tour_history_metas.append(meta)
        listbox.append(self._make_tour_history_row(meta))

    def _make_tour_history_row(self, meta: dict) -> Adw.ActionRow:
        action_row = Adw.ActionRow()
        action_row.set_title(GLib.markup_escape_text(self._format_history_title(meta)))
        action_row.set_subtitle(GLib.markup_escape_text(self._format_history_subtitle(meta)))
        action_row._dp_history_meta = meta
        key = (meta["kind"], meta["id"])

        if getattr(self, "_tour_history_select_mode", False):
            # In select mode the checkbox prefix tracks selection, but the
            # entire row is also activatable so a tap anywhere toggles —
            # the checkbox alone is too small a touch target.
            check = Gtk.CheckButton()
            check.set_active(key in self._tour_history_selected)
            check.set_valign(Gtk.Align.CENTER)
            check.connect("toggled", self._on_history_row_check_toggled, key)
            action_row.add_prefix(check)
            action_row.set_activatable(True)
            action_row.connect(
                "activated", lambda _r, c=check: c.set_active(not c.get_active())
            )
            return action_row

        loaded_tour_id = getattr(self, "_loaded_tour_id", None)
        loaded_trip_id = getattr(self, "_loaded_trip_id", None)
        is_loaded = (
            meta["kind"] == "tour"
            and loaded_tour_id is not None
            and int(meta["id"]) == loaded_tour_id
        ) or (
            meta["kind"] == "trip"
            and loaded_trip_id is not None
            and int(meta["id"]) == loaded_trip_id
        )
        # Keep the kind-specific icon shape even when loaded; only tint
        # it green via the dp-tour-loaded-icon CSS class. Swapping to
        # emblem-ok-symbolic for the loaded row was unreliable on some
        # icon themes (icon failed to render → row looked blank).
        if meta["kind"] == "tour":
            icon = Gtk.Image.new_from_icon_name("dp-tour-plan-symbolic")
        else:
            icon = Gtk.Image.new_from_icon_name("distance-symbolic")
        if is_loaded:
            icon.add_css_class("dp-tour-loaded-icon")
        action_row.add_prefix(icon)

        edit_btn = Gtk.Button(icon_name="document-edit-symbolic")
        edit_btn.add_css_class("flat")
        edit_btn.add_css_class("circular")
        edit_btn.set_valign(Gtk.Align.CENTER)
        edit_btn.set_tooltip_text(_translate(self.language, "map.history.edit"))
        edit_btn.connect("clicked", self._on_history_row_edit_clicked, action_row)
        action_row.add_suffix(edit_btn)

        action_row.set_activatable(True)
        action_row.connect("activated", self._on_history_row_activated)

        # Long-press enters multi-select mode with this row pre-checked —
        # matches the gesture used on the cars trips/scans/photos lists.
        # Keep the default BUBBLE propagation phase so a normal tap still
        # reaches the row's "activated" signal (which loads the tour /
        # opens the replay).
        lp = Gtk.GestureLongPress()
        lp.set_touch_only(False)
        lp.connect(
            "pressed",
            lambda _g, _x, _y, k=key: self._enter_history_select_mode(k),
        )
        action_row.add_controller(lp)
        return action_row

    def _enter_history_select_mode(self, initial_key: tuple[str, int]) -> None:
        if self._tour_history_select_mode:
            return
        self._tour_history_select_mode = True
        self._tour_history_selected = {initial_key}
        self._rebuild_tour_history_rows()
        trash_btn = getattr(self, "_tour_history_trash_btn", None)
        if trash_btn is not None:
            trash_btn.set_visible(True)
        share_btn = getattr(self, "_tour_history_share_btn", None)
        if share_btn is not None:
            share_btn.set_visible(self._sync_active())

    def _exit_history_select_mode(self) -> None:
        if not self._tour_history_select_mode:
            return
        self._tour_history_select_mode = False
        self._tour_history_selected = set()
        self._rebuild_tour_history_rows()
        trash_btn = getattr(self, "_tour_history_trash_btn", None)
        if trash_btn is not None:
            trash_btn.set_visible(False)
        share_btn = getattr(self, "_tour_history_share_btn", None)
        if share_btn is not None:
            share_btn.set_visible(False)

    def _rebuild_tour_history_rows(self) -> None:
        listbox = getattr(self, "_tour_history_listbox", None)
        if listbox is None:
            return
        child = listbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            listbox.remove(child)
            child = nxt
        self._tour_history_empty_row = None
        for meta in self._tour_history_metas:
            listbox.append(self._make_tour_history_row(meta))
        if not self._tour_history_metas:
            self._show_tour_history_empty()

    def _on_history_row_check_toggled(
        self, check: Gtk.CheckButton, key: tuple[str, int]
    ) -> None:
        selected = getattr(self, "_tour_history_selected", None)
        if selected is None:
            return
        if check.get_active():
            selected.add(key)
        else:
            selected.discard(key)
        if not selected:
            self._exit_history_select_mode()

    def _on_history_row_edit_clicked(
        self, _btn: Gtk.Button, row: Adw.ActionRow
    ) -> None:
        meta = getattr(row, "_dp_history_meta", None)
        if not meta:
            return
        current_name = (
            meta.get("trip_label")
            or self._format_history_title(meta)
            or ""
        )

        entry = Gtk.Entry()
        entry.set_text(current_name)
        entry.set_activates_default(True)
        entry.set_placeholder_text(
            _translate(self.language, "map.history.rename_placeholder")
        )

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content_box.set_margin_top(6)
        content_box.set_margin_bottom(6)
        content_box.append(entry)

        dialog = Adw.AlertDialog(
            heading=_translate(self.language, "map.history.rename_title")
        )
        dialog.set_extra_child(content_box)
        dialog.add_response("cancel", _translate(self.language, "map.tours.cancel"))
        dialog.add_response("delete", _translate(self.language, "map.tours.delete_confirm"))
        dialog.add_response("save", _translate(self.language, "map.tours.do_save"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def _on_response(_d: Adw.AlertDialog, resp: str) -> None:
            if resp == "save":
                new_name = entry.get_text().strip()
                self._rename_history_entry(meta, new_name, row)
            elif resp == "delete":
                self._confirm_delete_history_entry(meta, row)

        dialog.connect("response", _on_response)
        dialog.present(self.get_root())

    def _rename_history_entry(
        self, meta: dict, new_name: str, row: Adw.ActionRow
    ) -> None:
        db = getattr(self, "_map_db", None)
        if db is None:
            return
        try:
            if meta["kind"] == "tour":
                db.rename_saved_tour(int(meta["id"]), new_name)
            else:
                db.rename_trip(int(meta["id"]), new_name)
        except sqlite3.Error:
            log.warning("Could not rename %s id=%s", meta.get("kind"), meta.get("id"), exc_info=True)
            return
        meta["trip_label"] = new_name
        row._dp_history_meta = meta
        row.set_title(GLib.markup_escape_text(self._format_history_title(meta)))

    def _confirm_delete_history_entry(
        self, meta: dict, row: Adw.ActionRow
    ) -> None:
        dialog = Adw.AlertDialog(
            heading=_translate(self.language, "map.history.delete_entry_heading"),
            body=_translate(self.language, "map.history.delete_entry_body"),
        )
        dialog.add_response("cancel", _translate(self.language, "map.tours.cancel"))
        dialog.add_response("delete", _translate(self.language, "map.tours.delete_confirm"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def _on_response(_d: Adw.AlertDialog, resp: str) -> None:
            if resp != "delete":
                return
            key = (meta["kind"], int(meta["id"]))
            self._delete_history_entries([key])
            self._tour_history_metas = [
                m for m in self._tour_history_metas
                if (m["kind"], int(m["id"])) != key
            ]
            listbox = getattr(self, "_tour_history_listbox", None)
            if listbox is not None:
                listbox.remove(row)

        dialog.connect("response", _on_response)
        dialog.present(self.get_root())

    def _on_history_trash_clicked(self, _btn: Gtk.Button) -> None:
        selected = getattr(self, "_tour_history_selected", None)
        if not selected:
            return
        keys = list(selected)
        body = _translate(
            self.language,
            "map.history.delete_selected_body",
            count=str(len(keys)),
        )
        dialog = Adw.AlertDialog(
            heading=_translate(self.language, "map.history.delete_selected_heading"),
            body=body,
        )
        dialog.add_response("cancel", _translate(self.language, "map.tours.cancel"))
        dialog.add_response("delete", _translate(self.language, "map.tours.delete_confirm"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def _on_response(_d: Adw.AlertDialog, resp: str) -> None:
            if resp != "delete":
                return
            self._delete_history_entries(keys)
            key_set = set(keys)
            self._tour_history_metas = [
                m for m in self._tour_history_metas
                if (m["kind"], int(m["id"])) not in key_set
            ]
            self._exit_history_select_mode()

        dialog.connect("response", _on_response)
        dialog.present(self.get_root())

    def _delete_history_entries(self, keys: list[tuple[str, int]]) -> None:
        db = getattr(self, "_map_db", None)
        if db is None:
            return
        for kind, eid in keys:
            try:
                if kind == "tour":
                    db.delete_saved_tour(eid)
                else:
                    db.delete_trip(eid)
            except sqlite3.Error:
                log.warning("Could not delete %s id=%s", kind, eid, exc_info=True)
                continue
            selected = getattr(self, "_tour_history_selected", None)
            if selected is not None:
                selected.discard((kind, eid))

    def _on_history_row_activated(self, row: Adw.ActionRow) -> None:
        meta = getattr(row, "_dp_history_meta", None)
        if not meta:
            return
        if meta["kind"] == "trip":
            self._show_trip_replay(meta)
        elif meta["kind"] == "tour":
            db = getattr(self, "_map_db", None)
            if db is None:
                return
            tour = db.get_saved_tour(int(meta["id"]))
            if tour is None:
                return
            # _load_saved_tour pops the nav view and routes via _on_route_clicked
            self._load_saved_tour(dict(tour))
