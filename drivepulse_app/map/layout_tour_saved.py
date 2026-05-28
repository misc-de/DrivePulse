"""Saved-tour list on MapPage: the "Load" page (long-press → bulk delete /
share), the "Save" dialog (save current waypoints as a new tour or update the
loaded one), per-row rename and per-row share.

Lives in its own mixin because the load list and save dialog share enough state
(``_saved_tour_*``, ``_loaded_tour_*``) that keeping them together makes the
flow easy to follow."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from gi.repository import Adw, GLib, Gtk

from drivepulse_app.common import _translate
from drivepulse_app.diagnostics import get_logger
from drivepulse_app.map._list_helpers import make_bulk_select_header, make_empty_dim_row

log = get_logger(__name__)


class MapTourSavedMixin:
    # Concrete MapPage state surfaced to this mixin. See project_mixin_typing.md.
    language: str
    _entry_rows: list[tuple[Gtk.Box, Gtk.Entry, Gtk.Widget]]
    get_root: Callable[[], Any]
    _sync_active: Callable[[], bool]
    _make_share_flow: Callable[[], Any]
    _on_route_clicked: Callable[..., Any]
    _on_saved_tour_share_clicked: Callable[..., Any]
    _clear_replay_overlays: Callable[..., Any]
    _update_placeholders: Callable[..., Any]
    _insert_entry_after: Callable[..., Any]
    _remove_entry: Callable[..., Any]

    # Owning class (MapPage) initializes these in __init__. Annotated here so
    # mypy doesn't infer them as non-Optional from the assignments below.
    _loaded_tour_id: int | None
    _loaded_tour_name: str | None

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

        header, trash_btn, share_btn = make_bulk_select_header(
            trash_tooltip=_translate(self.language, "map.history.delete_selected_tooltip"),
            share_tooltip=_translate(self.language, "map.history.share_selected_tooltip"),
            on_trash=self._on_saved_tour_trash_clicked,
            on_share=self._on_saved_tour_share_clicked,
        )
        self._saved_tour_trash_btn = trash_btn
        self._saved_tour_share_btn = share_btn

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(header)
        toolbar_view.set_content(page_box)

        page = Adw.NavigationPage(title=_translate(self.language, "map.topnav.load"))
        page.set_child(toolbar_view)
        nav_view.push(page)
        self._rebuild_tour_list()

    def _on_tour_save_clicked(self, _btn: object) -> None:
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
            wp_json = json.dumps(waypoints)
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
            self._tour_listbox.append(
                make_empty_dim_row(_translate(self.language, "map.tours.empty"))
            )
            return

        for tour in self._saved_tour_metas:
            self._tour_listbox.append(self._make_saved_tour_row(tour))

    def _make_saved_tour_row(self, tour: dict) -> Adw.ActionRow:
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
            row.set_activatable(True)
            row.connect(
                "activated", lambda _r, c=check: c.set_active(not c.get_active())
            )
            return row

        loaded_id = getattr(self, "_loaded_tour_id", None)
        icon = Gtk.Image.new_from_icon_name("dp-tour-plan-symbolic")
        if loaded_id is not None and int(tour["id"]) == loaded_id:
            # Same icon as the unloaded state, just tinted via the
            # dp-tour-loaded-icon CSS class. Using a different icon name
            # here (emblem-ok-symbolic) was unreliable across icon
            # themes — on some setups the icon simply didn't render,
            # so the loaded row ended up blank.
            icon.add_css_class("dp-tour-loaded-icon")
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
        waypoints: list[str] = json.loads(tour["waypoints_json"])

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
