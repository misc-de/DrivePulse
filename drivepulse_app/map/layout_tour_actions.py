"""Map page tour-management actions — topnav (Load/Plan/Save/History) and saved-tour list."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC
from typing import Any

from gi.repository import Adw, GLib, Gtk

from drivepulse_app.common import _translate
from drivepulse_app.diagnostics import get_logger
from drivepulse_app.map.layout_css import _install_maneuver_css

log = get_logger(__name__)


class MapTourActionsMixin:
    """Top navigation bar above the map plus the dialogs/lists for saved tours
    and tour/trip history."""

    # Pull this many history rows per fetch. Small enough to keep the first
    # render snappy, large enough that you don't trigger pagination on every
    # tiny scroll.
    _TOUR_HISTORY_PAGE_SIZE = 30

    # Declared here so mypy widens the inferred attribute type across the mixin
    # chain. Owning class (MapPage) initialises the concrete value in __init__.
    _loaded_tour_name: str | None

    def _build_tour_topnav(self) -> None:
        _install_maneuver_css()
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        bar.add_css_class("dp-tour-topnav")
        bar.set_margin_start(4)
        bar.set_margin_end(4)
        bar.set_margin_top(4)
        bar.set_margin_bottom(4)
        self._tour_topnav = bar

        def _child(icon_name: str, label_key: str) -> Gtk.Box:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.set_halign(Gtk.Align.CENTER)
            img = Gtk.Image.new_from_icon_name(icon_name)
            img.set_pixel_size(22)
            lbl = Gtk.Label(label=_translate(self.language, label_key))
            lbl.add_css_class("caption")
            box.append(img)
            box.append(lbl)
            return box

        load_btn = Gtk.Button()
        load_btn.set_child(_child("document-open-symbolic", "map.topnav.load"))
        load_btn.add_css_class("flat")
        load_btn.set_hexpand(True)
        load_btn.connect("clicked", self._on_tour_load_clicked)
        self._tour_load_btn = load_btn

        plan_btn = Gtk.ToggleButton()
        plan_btn.set_child(_child("distance-symbolic", "map.topnav.plan"))
        plan_btn.add_css_class("flat")
        plan_btn.set_hexpand(True)
        plan_btn.connect("toggled", self._on_tour_plan_toggled)
        self._tour_plan_btn = plan_btn

        save_btn = Gtk.Button()
        save_btn.set_child(_child("document-save-symbolic", "map.topnav.save"))
        save_btn.add_css_class("flat")
        save_btn.set_hexpand(True)
        save_btn.connect("clicked", self._on_tour_save_clicked)
        save_btn.set_visible(False)
        self._tour_save_btn = save_btn

        history_btn = Gtk.Button()
        history_btn.set_child(_child("document-open-recent-symbolic", "map.topnav.history"))
        history_btn.add_css_class("flat")
        history_btn.set_hexpand(True)
        history_btn.connect("clicked", self._on_tour_history_clicked)
        self._tour_history_btn = history_btn

        # "Letzte Touren" sits on the far left as a view-only entry point;
        # the tour-planning actions (load / plan / save) follow on the right.
        for btn in (history_btn, load_btn, plan_btn, save_btn):
            bar.append(btn)

        self._map_content_box.append(bar)

    def _on_tour_plan_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._tour_plan_active = btn.get_active()
        if self._search_bar is not None:
            self._search_bar.set_visible(self._tour_plan_active)
        # Keep an already-loaded tour visible on the map while the user edits
        # waypoints; the route is only cleared once "Calculate route" runs
        # (see _on_route_clicked, which clears overlays before re-routing).
        GLib.idle_add(self._nudge_map_resize)

    def _on_tour_load_clicked(self, _btn: object) -> None:
        nav_view = getattr(self, "_nav_view", None)
        if nav_view is None:
            return

        # Mirrors the history list: per-row icon + edit button by default;
        # long-press enters select mode with checkbox prefixes + a header
        # trash button for bulk delete.
        self._saved_tour_select_mode: bool = False
        self._saved_tour_selected: set[int] = set()
        self._saved_tour_metas: list[dict] = []

        page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        page_box.set_hexpand(True)
        page_box.set_vexpand(True)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        listbox.set_valign(Gtk.Align.START)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        inner.set_margin_top(12)
        inner.set_margin_bottom(12)
        inner.set_margin_start(12)
        inner.set_margin_end(12)
        inner.append(listbox)
        scrolled.set_child(inner)
        page_box.append(scrolled)

        self._tour_listbox = listbox

        header = Adw.HeaderBar()
        trash_btn = Gtk.Button(icon_name="user-trash-symbolic")
        trash_btn.add_css_class("destructive-action")
        trash_btn.set_tooltip_text(
            _translate(self.language, "map.history.delete_selected_tooltip")
        )
        trash_btn.set_visible(False)
        trash_btn.connect("clicked", self._on_saved_tour_trash_clicked)
        self._saved_tour_trash_btn = trash_btn
        header.pack_end(trash_btn)

        share_btn = Gtk.Button(icon_name="share-alt-symbolic")
        share_btn.add_css_class("flat")
        share_btn.set_tooltip_text(
            _translate(self.language, "map.history.share_selected_tooltip")
        )
        share_btn.set_visible(False)
        share_btn.connect("clicked", self._on_saved_tour_share_clicked)
        self._saved_tour_share_btn = share_btn
        header.pack_end(share_btn)

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(header)
        toolbar_view.set_content(page_box)

        page = Adw.NavigationPage(title=_translate(self.language, "map.topnav.load"))
        page.set_child(toolbar_view)
        nav_view.push(page)
        self._rebuild_tour_list()

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

        header = Adw.HeaderBar()
        trash_btn = Gtk.Button(icon_name="user-trash-symbolic")
        trash_btn.add_css_class("destructive-action")
        trash_btn.set_tooltip_text(
            _translate(self.language, "map.history.delete_selected_tooltip")
        )
        trash_btn.set_visible(False)
        trash_btn.connect("clicked", self._on_history_trash_clicked)
        self._tour_history_trash_btn = trash_btn
        header.pack_end(trash_btn)

        share_btn = Gtk.Button(icon_name="share-alt-symbolic")
        share_btn.add_css_class("flat")
        share_btn.set_tooltip_text(
            _translate(self.language, "map.history.share_selected_tooltip")
        )
        share_btn.set_visible(False)
        share_btn.connect("clicked", self._on_history_share_clicked)
        self._tour_history_share_btn = share_btn
        header.pack_end(share_btn)

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
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_selectable(False)
        lbl = Gtk.Label(label=_translate(self.language, "map.history.empty"))
        lbl.add_css_class("dim-label")
        lbl.set_margin_top(18)
        lbl.set_margin_bottom(18)
        lbl.set_wrap(True)
        row.set_child(lbl)
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
            # In select mode the row is non-activatable; checkbox prefix
            # toggles selection. No edit button — that lives in normal mode.
            check = Gtk.CheckButton()
            check.set_active(key in self._tour_history_selected)
            check.set_valign(Gtk.Align.CENTER)
            check.connect("toggled", self._on_history_row_check_toggled, key)
            action_row.add_prefix(check)
            action_row.set_activatable(False)
            return action_row

        loaded_id = getattr(self, "_loaded_tour_id", None)
        is_loaded = (
            meta["kind"] == "tour"
            and loaded_id is not None
            and int(meta["id"]) == loaded_id
        )
        if is_loaded:
            icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
            icon.add_css_class("dp-tour-loaded-icon")
        elif meta["kind"] == "tour":
            icon = Gtk.Image.new_from_icon_name("dp-tour-plan-symbolic")
        else:
            icon = Gtk.Image.new_from_icon_name("distance-symbolic")
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

    def _on_tour_save_clicked(self, _btn: object) -> None:
        import json as _json
        from datetime import datetime
        db = getattr(self, "_map_db", None)
        if db is None:
            return

        waypoints = [e.get_text().strip() for _, e, __ in self._entry_rows]
        loaded_name = getattr(self, "_loaded_tour_name", None)
        if loaded_name:
            default_name = loaded_name
        else:
            names = [w for w in waypoints if w]
            if len(names) >= 2:
                default_name = f"{names[0]} → {names[-1]}"
            elif names:
                default_name = names[0]
            else:
                default_name = datetime.now().strftime("%d.%m.%Y")

        _raw_loaded_id: int | None = getattr(self, "_loaded_tour_id", None)
        loaded_id: int | None = (
            _raw_loaded_id
            if _raw_loaded_id is not None and db is not None
            and db.get_saved_tour(_raw_loaded_id) is not None
            else None
        )

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content_box.set_margin_top(6)
        content_box.set_margin_bottom(6)

        name_entry = Gtk.Entry()
        name_entry.set_text(default_name)
        name_entry.set_activates_default(True)
        content_box.append(name_entry)

        save_new_check: Gtk.CheckButton | None = None
        if loaded_id is not None:
            save_new_check = Gtk.CheckButton(
                label=_translate(self.language, "map.tours.save_as_new")
            )
            save_new_check.set_active(False)
            content_box.append(save_new_check)

        dialog = Adw.AlertDialog(heading=_translate(self.language, "map.topnav.save"))
        dialog.set_extra_child(content_box)
        dialog.add_response("cancel", _translate(self.language, "map.tours.cancel"))
        dialog.add_response("save", _translate(self.language, "map.tours.do_save"))
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def _on_response(_d: Adw.AlertDialog, resp: str) -> None:
            if resp != "save":
                return
            name = name_entry.get_text().strip() or default_name
            as_new = save_new_check is None or save_new_check.get_active()
            wp_json = _json.dumps(waypoints)
            now = datetime.now(UTC).isoformat()
            if not as_new and loaded_id is not None:
                db.update_saved_tour(loaded_id, name, wp_json)
            else:
                tid = db.save_tour(name, wp_json, now)
                self._loaded_tour_id = tid

        dialog.connect("response", _on_response)
        dialog.present(self.get_root())

    def _rebuild_tour_list(self) -> None:
        if self._tour_listbox is None:
            return
        child = self._tour_listbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._tour_listbox.remove(child)
            child = nxt

        db = getattr(self, "_map_db", None)
        tours = db.list_saved_tours() if db is not None else []
        self._saved_tour_metas = [dict(t) for t in tours]

        if not tours:
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            row.set_selectable(False)
            lbl = Gtk.Label(label=_translate(self.language, "map.tours.empty"))
            lbl.add_css_class("dim-label")
            lbl.set_margin_top(14)
            lbl.set_margin_bottom(14)
            row.set_child(lbl)
            self._tour_listbox.append(row)
            return

        for tour in self._saved_tour_metas:
            self._tour_listbox.append(self._make_saved_tour_row(tour))

    def _make_saved_tour_row(self, tour: dict) -> Adw.ActionRow:
        from datetime import datetime
        row = Adw.ActionRow()
        try:
            dt = datetime.fromisoformat(tour["created_at"])
            date_str = dt.strftime("%d.%m.%Y %H:%M")
        except (ValueError, TypeError, KeyError):
            log.debug("Unparseable tour.created_at=%r", tour.get("created_at"), exc_info=True)
            date_str = str(tour.get("created_at", ""))[:16]
        row.set_title(GLib.markup_escape_text(str(tour["name"])))
        row.set_subtitle(GLib.markup_escape_text(date_str))
        row._dp_tour = tour
        tour_id = int(tour["id"])

        if getattr(self, "_saved_tour_select_mode", False):
            check = Gtk.CheckButton()
            check.set_active(tour_id in self._saved_tour_selected)
            check.set_valign(Gtk.Align.CENTER)
            check.connect("toggled", self._on_saved_tour_check_toggled, tour_id)
            row.add_prefix(check)
            row.set_activatable(False)
            return row

        loaded_id = getattr(self, "_loaded_tour_id", None)
        if loaded_id is not None and int(tour["id"]) == loaded_id:
            icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
            icon.add_css_class("dp-tour-loaded-icon")
        else:
            icon = Gtk.Image.new_from_icon_name("dp-tour-plan-symbolic")
        row.add_prefix(icon)

        sync_getter = getattr(self, "get_sync_client", None)
        sync_active = callable(sync_getter) and sync_getter() is not None
        if sync_active:
            share_btn = Gtk.Button(icon_name="share-alt-symbolic")
            share_btn.add_css_class("flat")
            share_btn.add_css_class("circular")
            share_btn.set_valign(Gtk.Align.CENTER)
            share_btn.connect(
                "clicked", lambda _b, td=tour: self._share_saved_tour(td)
            )
            row.add_suffix(share_btn)

        edit_btn = Gtk.Button(icon_name="document-edit-symbolic")
        edit_btn.add_css_class("flat")
        edit_btn.add_css_class("circular")
        edit_btn.set_valign(Gtk.Align.CENTER)
        edit_btn.set_tooltip_text(_translate(self.language, "map.history.edit"))
        edit_btn.connect("clicked", self._on_saved_tour_edit_clicked, row)
        row.add_suffix(edit_btn)

        row.set_activatable(True)
        row.connect(
            "activated", lambda _r, td=tour: self._load_saved_tour(td)
        )

        lp = Gtk.GestureLongPress()
        lp.set_touch_only(False)
        lp.connect(
            "pressed",
            lambda _g, _x, _y, tid=tour_id: self._enter_saved_tour_select_mode(tid),
        )
        row.add_controller(lp)
        return row

    def _enter_saved_tour_select_mode(self, initial_id: int) -> None:
        if self._saved_tour_select_mode:
            return
        self._saved_tour_select_mode = True
        self._saved_tour_selected = {initial_id}
        self._rebuild_tour_list()
        trash_btn = getattr(self, "_saved_tour_trash_btn", None)
        if trash_btn is not None:
            trash_btn.set_visible(True)
        share_btn = getattr(self, "_saved_tour_share_btn", None)
        if share_btn is not None:
            share_btn.set_visible(self._sync_active())

    def _exit_saved_tour_select_mode(self) -> None:
        if not self._saved_tour_select_mode:
            return
        self._saved_tour_select_mode = False
        self._saved_tour_selected = set()
        self._rebuild_tour_list()
        trash_btn = getattr(self, "_saved_tour_trash_btn", None)
        if trash_btn is not None:
            trash_btn.set_visible(False)
        share_btn = getattr(self, "_saved_tour_share_btn", None)
        if share_btn is not None:
            share_btn.set_visible(False)

    def _on_saved_tour_check_toggled(
        self, check: Gtk.CheckButton, tour_id: int
    ) -> None:
        if check.get_active():
            self._saved_tour_selected.add(tour_id)
        else:
            self._saved_tour_selected.discard(tour_id)
        if not self._saved_tour_selected:
            self._exit_saved_tour_select_mode()

    def _on_saved_tour_edit_clicked(
        self, _btn: Gtk.Button, row: Adw.ActionRow
    ) -> None:
        tour = getattr(row, "_dp_tour", None)
        if not tour:
            return
        tour_id = int(tour["id"])
        current_name = str(tour.get("name") or "")

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
                db = getattr(self, "_map_db", None)
                if db is not None and new_name:
                    try:
                        db.rename_saved_tour(tour_id, new_name)
                    except sqlite3.Error:
                        log.warning("Could not rename saved tour id=%s", tour_id, exc_info=True)
                        return
                    self._rebuild_tour_list()
            elif resp == "delete":
                # _delete_saved_tour already shows its own confirm dialog.
                self._delete_saved_tour(tour_id)

        dialog.connect("response", _on_response)
        dialog.present(self.get_root())

    def _on_saved_tour_trash_clicked(self, _btn: Gtk.Button) -> None:
        ids = list(getattr(self, "_saved_tour_selected", []))
        if not ids:
            return
        body = _translate(
            self.language,
            "map.history.delete_selected_body",
            count=str(len(ids)),
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
            db = getattr(self, "_map_db", None)
            if db is not None:
                for tid in ids:
                    try:
                        db.delete_saved_tour(int(tid))
                    except sqlite3.Error:
                        log.warning("Could not delete saved tour id=%s", tid, exc_info=True)
                        continue
            self._exit_saved_tour_select_mode()

        dialog.connect("response", _on_response)
        dialog.present(self.get_root())

    def _load_saved_tour(self, tour: dict) -> None:
        import json as _json
        waypoints: list[str] = _json.loads(tour["waypoints_json"])

        # Adjust entry row count to match saved waypoints (min 2)
        target = max(2, len(waypoints))
        while len(self._entry_rows) < target:
            self._insert_entry_after(self._entry_rows[-1][0])
        while len(self._entry_rows) > target:
            self._remove_entry(self._entry_rows[-1][0])

        self._loading_tour = True
        for (_, entry, __), text in zip(self._entry_rows, waypoints, strict=False):
            entry.set_text(text)
        self._loading_tour = False
        self._update_placeholders()

        self._loaded_tour_id = int(tour["id"])
        self._loaded_tour_name = str(tour.get("name") or "")
        self._clear_replay_overlays()
        nav_view = getattr(self, "_nav_view", None)
        if nav_view is not None:
            nav_view.pop()
        self._on_route_clicked(None)

    def _delete_saved_tour(self, tour_id: int) -> None:
        dialog = Adw.AlertDialog(
            heading=_translate(self.language, "map.tours.delete_heading"),
            body=_translate(self.language, "map.tours.delete_body"),
        )
        dialog.add_response("cancel", _translate(self.language, "map.tours.cancel"))
        dialog.add_response("delete", _translate(self.language, "map.tours.delete_confirm"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def _on_response(_d: Adw.AlertDialog, resp: str) -> None:
            if resp != "delete":
                return
            db = getattr(self, "_map_db", None)
            if db is not None:
                db.delete_saved_tour(tour_id)
            self._rebuild_tour_list()

        dialog.connect("response", _on_response)
        dialog.present(self.get_root())

    def _share_saved_tour(self, tour: dict) -> None:
        dialog = Adw.AlertDialog(
            heading=_translate(self.language, "share.tour_confirm_title"),
            body=_translate(self.language, "share.tour_confirm_body"),
        )
        dialog.add_response("cancel", _translate(self.language, "share.cancel"))
        dialog.add_response("send", _translate(self.language, "share.send"))
        dialog.set_response_appearance("send", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("send")
        dialog.set_close_response("cancel")

        def _on_response(_d: Adw.AlertDialog, resp: str) -> None:
            if resp != "send":
                return
            self._make_share_flow().share_tour(tour)

        dialog.connect("response", _on_response)
        dialog.present(self.get_root())

    # ── Bulk share from select mode ──────────────────────────────────────────

    def _sync_active(self) -> bool:
        """True when the sync client is configured and available — gates the
        bulk share-button. Mirrors the per-row gate in _make_saved_tour_row."""
        sync_getter = getattr(self, "get_sync_client", None)
        return callable(sync_getter) and sync_getter() is not None

    def notify_sync_changed(self) -> None:
        """Sync-Status hat sich geändert — Tour-Listen und Share-Buttons neu aufbauen."""
        try:
            self._rebuild_tour_list()
            self._rebuild_tour_history_rows()
        except Exception:
            pass

    def _make_share_flow(self) -> Any:
        from drivepulse_app.share.flow import ShareFlow
        return ShareFlow(
            self, self._map_db, self.language, getattr(self, "get_sync_client", None)
        )

    def _on_saved_tour_share_clicked(self, _btn: Gtk.Button) -> None:
        ids = list(getattr(self, "_saved_tour_selected", []))
        if not ids:
            return
        # Resolve full tour rows from the list of metas built in _rebuild_tour_list.
        id_set = set(ids)
        tours = [t for t in self._saved_tour_metas if int(t["id"]) in id_set]
        if not tours:
            return
        self._confirm_and_bulk_share(
            count=len(tours),
            on_send=lambda: (
                self._make_share_flow().share_tours(tours),
                self._exit_saved_tour_select_mode(),
            ),
        )

    def _on_history_share_clicked(self, _btn: Gtk.Button) -> None:
        selected = getattr(self, "_tour_history_selected", None)
        if not selected:
            return
        key_set = set(selected)
        metas = [
            m for m in self._tour_history_metas
            if (m["kind"], int(m["id"])) in key_set
        ]
        if not metas:
            return

        # Split into saved-tours (one batched payload) and trips (one batch
        # per owning car_id, since share_trips runs the per-vehicle handshake).
        tour_ids = [int(m["id"]) for m in metas if m["kind"] == "tour"]
        trips_by_car: dict[int, list[int]] = {}
        for m in metas:
            if m["kind"] != "trip":
                continue
            cid = m.get("car_id")
            if cid is None:
                continue
            trips_by_car.setdefault(int(cid), []).append(int(m["id"]))

        if not tour_ids and not trips_by_car:
            return

        def _do_send() -> None:
            flow = self._make_share_flow()
            if tour_ids:
                db = getattr(self, "_map_db", None)
                tour_rows: list[dict] = []
                if db is not None:
                    for tid in tour_ids:
                        row = db.get_saved_tour(tid)
                        if row is None:
                            continue
                        tour_rows.append({
                            "id": row["id"],
                            "name": row["name"],
                            "created_at": row["created_at"],
                            "waypoints_json": row["waypoints_json"],
                        })
                if tour_rows:
                    flow.share_tours(tour_rows)
            for car_id, trip_ids in trips_by_car.items():
                flow.share_trips(car_id, trip_ids)
            self._exit_history_select_mode()

        self._confirm_and_bulk_share(count=len(metas), on_send=_do_send)

    def _confirm_and_bulk_share(self, count: int, on_send: Callable[[], object]) -> None:
        dialog = Adw.AlertDialog(
            heading=_translate(self.language, "share.selected_confirm_heading"),
            body=_translate(self.language, "share.selected_confirm_body", count=str(count)),
        )
        dialog.add_response("cancel", _translate(self.language, "share.cancel"))
        dialog.add_response("send", _translate(self.language, "share.send"))
        dialog.set_response_appearance("send", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("send")
        dialog.set_close_response("cancel")

        def _on_response(_d: Adw.AlertDialog, resp: str) -> None:
            if resp == "send":
                on_send()

        dialog.connect("response", _on_response)
        dialog.present(self.get_root())
