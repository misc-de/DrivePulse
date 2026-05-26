"""Share-conflict badge in the header and the modal page that lets the user
discard or apply each pending conflict row."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from gi.repository import Adw, Gtk

from drivepulse_app.common import _translate
from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)


class DashboardConflictsMixin:
    def _update_conflict_badge(self) -> None:
        try:
            n = self.db.count_share_conflicts()
        except sqlite3.Error:
            log.debug("Could not count share conflicts", exc_info=True)
            n = 0
        btn = getattr(self, "_conflict_btn", None)
        if btn is not None:
            btn.set_visible(n > 0)

    def _open_conflict_page(self, *_args: Any) -> None:
        if self.nav_view.find_page("share-conflicts") is not None:
            return

        def t(key: str, **values: object) -> str:
            return _translate(self.language, key, **values)

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=t("share.conflicts_title")))
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        toolbar_view.add_top_bar(header)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        outer.set_margin_top(16)
        outer.set_margin_bottom(16)
        outer.set_margin_start(16)
        outer.set_margin_end(16)

        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        list_box.add_css_class("boxed-list")
        list_box.set_valign(Gtk.Align.START)
        outer.append(list_box)

        def _refresh() -> None:
            while True:
                child = list_box.get_first_child()
                if child is None:
                    break
                list_box.remove(child)
            try:
                conflicts = self.db.list_share_conflicts()
            except sqlite3.Error:
                log.warning("Could not list share conflicts", exc_info=True)
                conflicts = []
            for c in conflicts:
                try:
                    incoming = json.loads(c["incoming_json"])
                except (ValueError, TypeError, json.JSONDecodeError):
                    log.debug("Conflict id=%s has unparseable incoming_json", c["id"], exc_info=True)
                    incoming = {}
                typ = c["type"]
                type_label = {
                    "trip": t("share.conflict_type_trip"),
                    "run": t("share.conflict_type_run"),
                    "scan": t("share.conflict_type_scan"),
                }.get(typ, typ)
                item_ts_raw = (
                    incoming.get("started_at")
                    or incoming.get("run_at")
                    or incoming.get("scanned_at")
                    or ""
                )
                item_ts = item_ts_raw[:16].replace("T", " ") if item_ts_raw else ""
                row = Adw.ActionRow()
                row.set_title(type_label + (f"  {item_ts}" if item_ts else ""))
                received = c["received_at"][:16].replace("T", " ") if c["received_at"] else ""
                row.set_subtitle(t("share.conflict_received", ts=received) if received else "")

                cid = int(c["id"])

                discard_btn = Gtk.Button(label=t("share.conflict_discard"))
                discard_btn.add_css_class("flat")
                discard_btn.set_valign(Gtk.Align.CENTER)

                def _discard(_btn: Gtk.Button, conflict_id: int = cid) -> None:
                    try:
                        self.db.discard_conflict(conflict_id)
                    except sqlite3.Error:
                        log.warning("Could not discard conflict id=%s", conflict_id, exc_info=True)
                    _refresh()
                    self._update_conflict_badge()

                discard_btn.connect("clicked", _discard)
                row.add_suffix(discard_btn)

                apply_btn = Gtk.Button(label=t("share.conflict_apply"))
                apply_btn.add_css_class("suggested-action")
                apply_btn.set_valign(Gtk.Align.CENTER)

                def _apply(_btn: Gtk.Button, conflict_id: int = cid) -> None:
                    try:
                        self.db.resolve_conflict(conflict_id)
                    except sqlite3.Error:
                        log.warning("Could not resolve conflict id=%s", conflict_id, exc_info=True)
                    _refresh()
                    self._update_conflict_badge()
                    self.cars_page.refresh_profiles()

                apply_btn.connect("clicked", _apply)
                row.add_suffix(apply_btn)
                list_box.append(row)

        _refresh()

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(outer)
        toolbar_view.set_content(scroll)

        page = Adw.NavigationPage()
        page.set_tag("share-conflicts")
        page.set_title(t("share.conflicts_title"))
        page.set_child(toolbar_view)
        self.nav_view.push(page)
