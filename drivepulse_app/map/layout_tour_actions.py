"""Map page tour-management actions — topnav (Load/Plan/Save/History) and saved-tour list."""
from __future__ import annotations

from datetime import UTC
from typing import Any

from gi.repository import Adw, GLib, Gtk

from drivepulse_app.common import _translate
from drivepulse_app.map.layout_css import _install_maneuver_css


class MapTourActionsMixin:
    """Top navigation bar above the map plus the dialogs/lists for saved tours
    and tour/trip history."""

    # Pull this many history rows per fetch. Small enough to keep the first
    # render snappy, large enough that you don't trigger pagination on every
    # tiny scroll.
    _TOUR_HISTORY_PAGE_SIZE = 30

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
        if self._tour_plan_active:
            # Drop any trip-replay polyline / overlays so the planning UI starts clean.
            self._clear_replay_overlays()
        GLib.idle_add(self._nudge_map_resize)

    def _on_tour_load_clicked(self, _btn: object) -> None:
        nav_view = getattr(self, "_nav_view", None)
        if nav_view is None:
            return
        page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        page_box.set_hexpand(True)
        page_box.set_vexpand(True)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("navigation-sidebar")
        scrolled.set_child(listbox)
        page_box.append(scrolled)

        self._tour_listbox = listbox

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(Adw.HeaderBar())
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
        action_row = Adw.ActionRow()
        action_row.set_title(GLib.markup_escape_text(self._format_history_title(data)))
        action_row.set_subtitle(GLib.markup_escape_text(self._format_history_subtitle(data)))

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
        }
        action_row._dp_history_meta = meta
        key = (meta["kind"], meta["id"])

        # Selection checkbox — leftmost prefix.
        check = Gtk.CheckButton()
        check.set_valign(Gtk.Align.CENTER)
        check.connect("toggled", self._on_history_row_check_toggled, key)
        action_row._dp_check = check
        action_row.add_prefix(check)

        icon_name = (
            "dp-tour-plan-symbolic" if data["kind"] == "tour" else "driving-symbolic"
        )
        icon = Gtk.Image.new_from_icon_name(icon_name)
        action_row.add_prefix(icon)

        # Edit (pencil) button replaces the old chevron suffix.
        edit_btn = Gtk.Button(icon_name="document-edit-symbolic")
        edit_btn.add_css_class("flat")
        edit_btn.add_css_class("circular")
        edit_btn.set_valign(Gtk.Align.CENTER)
        edit_btn.set_tooltip_text(_translate(self.language, "map.history.edit"))
        edit_btn.connect("clicked", self._on_history_row_edit_clicked, action_row)
        action_row.add_suffix(edit_btn)

        action_row.set_activatable(True)
        action_row.connect("activated", self._on_history_row_activated)
        listbox.append(action_row)

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
        trash_btn = getattr(self, "_tour_history_trash_btn", None)
        if trash_btn is not None:
            trash_btn.set_visible(bool(selected))

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
        except Exception:
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
            self._delete_history_entries([(meta["kind"], int(meta["id"]))])
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
            listbox = getattr(self, "_tour_history_listbox", None)
            if listbox is None:
                return
            child = listbox.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                if isinstance(child, Adw.ActionRow):
                    m = getattr(child, "_dp_history_meta", None)
                    if m and (m["kind"], int(m["id"])) in set(keys):
                        listbox.remove(child)
                child = nxt
            selected.clear()
            trash_btn = getattr(self, "_tour_history_trash_btn", None)
            if trash_btn is not None:
                trash_btn.set_visible(False)

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
            except Exception:
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

        for tour in tours:
            tour_id = int(tour["id"])
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(tour["created_at"])
                date_str = dt.strftime("%d.%m.%Y %H:%M")
            except Exception:
                date_str = str(tour["created_at"])[:16]

            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            row_box.set_margin_start(4)
            row_box.set_margin_end(4)
            row_box.set_margin_top(2)
            row_box.set_margin_bottom(2)

            text_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            text_col.set_hexpand(True)
            text_col.set_valign(Gtk.Align.CENTER)
            name_lbl = Gtk.Label(label=str(tour["name"]), xalign=0.0)
            name_lbl.add_css_class("dp-steps-instr")
            name_lbl.set_max_width_chars(24)
            name_lbl.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
            date_lbl = Gtk.Label(label=date_str, xalign=0.0)
            date_lbl.add_css_class("dim-label")
            date_lbl.add_css_class("caption")
            text_col.append(name_lbl)
            text_col.append(date_lbl)

            load_btn = Gtk.Button()
            load_btn.add_css_class("flat")
            load_btn.set_hexpand(True)
            load_btn.set_child(text_col)
            tour_data = dict(tour)
            load_btn.connect("clicked", lambda _b, td=tour_data: self._load_saved_tour(td))

            sync_getter = getattr(self, "get_sync_client", None)
            sync_active = callable(sync_getter) and sync_getter() is not None
            if sync_active:
                share_btn = Gtk.Button(icon_name="share-alt-symbolic")
                share_btn.add_css_class("flat")
                share_btn.add_css_class("circular")
                share_btn.set_valign(Gtk.Align.CENTER)
                share_btn.connect("clicked", lambda _b, td=tour_data: self._share_saved_tour(td))
            else:
                share_btn = None

            del_btn = Gtk.Button(icon_name="user-trash-symbolic")
            del_btn.add_css_class("flat")
            del_btn.add_css_class("circular")
            del_btn.set_valign(Gtk.Align.CENTER)
            del_btn.connect("clicked", lambda _b, tid=tour_id: self._delete_saved_tour(tid))

            row_box.append(load_btn)
            if share_btn is not None:
                row_box.append(share_btn)
            row_box.append(del_btn)

            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            row.set_selectable(False)
            row.set_child(row_box)
            self._tour_listbox.append(row)

    def _load_saved_tour(self, tour: dict) -> None:
        import json as _json
        waypoints: list[str] = _json.loads(tour["waypoints_json"])

        # Adjust entry row count to match saved waypoints (min 2)
        target = max(2, len(waypoints))
        while len(self._entry_rows) < target:
            self._insert_entry_after(self._entry_rows[-1][0])
        while len(self._entry_rows) > target:
            self._remove_entry(self._entry_rows[-1][0])

        for (_, entry, __), text in zip(self._entry_rows, waypoints, strict=False):
            entry.set_text(text)
        self._update_placeholders()

        self._loaded_tour_id = int(tour["id"])
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
            from drivepulse_app.share.flow import ShareFlow
            ShareFlow(self, self._map_db, self.language, getattr(self, "get_sync_client", None)).share_tour(tour)

        dialog.connect("response", _on_response)
        dialog.present(self.get_root())
