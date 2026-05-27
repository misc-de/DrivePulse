"""Scan list and detail navigation helpers for CarsPage."""
from __future__ import annotations

from typing import Any

from gi.repository import Adw, GLib, Gtk

from drivepulse_app.cars.scan_widgets import _build_scan_detail_widget, _safe_int
from drivepulse_app.common import _translate
from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)


class CarsScansMixin:
    # Concrete CarsPage initializes _scan_id_shown as Optional[int]; mypy
    # would infer ``int`` from the assignment at line 142 in this mixin.
    _scan_id_shown: int | None

    def _render_scans_into_value_list(self) -> None:
        if self.db is None or self._selected_car_id is None:
            self.value_list.append(self._info_row(_translate(self.language, "cars.scans.empty")))
            return
        try:
            scans = self.db.list_scans_for_car(self._selected_car_id)
        except Exception:
            log.exception("Could not list scans for car id=%s", self._selected_car_id)
            scans = []
        # Nur Scan-Läufe mit erfassten Sensordaten anzeigen
        scans = [s for s in scans if _safe_int(s["pids_count"]) > 0]
        if not scans:
            self.value_list.append(self._info_row(_translate(self.language, "cars.scans.empty")))
            return
        for scan in scans:
            self.value_list.append(self._make_scan_row(scan))

    def _make_scan_row(self, scan: Any) -> Adw.ActionRow:
        row = Adw.ActionRow()
        ts = self._parse_ts(scan["scanned_at"])
        title = ts.strftime("%d.%m.%Y · %H:%M") if ts else str(scan["id"])
        row.set_title(GLib.markup_escape_text(title))

        keys = scan.keys() if hasattr(scan, "keys") else []
        shared_at = scan["shared_at"] if "shared_at" in keys else None
        seen_at = scan["seen_at"] if "seen_at" in keys else None
        if shared_at and not seen_at:
            dot = Gtk.Label(label="●")
            dot.add_css_class("dp-new-dot")
            dot.set_valign(Gtk.Align.CENTER)
            row.add_prefix(dot)

        dtc = _safe_int(scan["dtc_count"])
        pids = _safe_int(scan["pids_count"])

        subtitle = f"{pids} {_translate(self.language, 'cars.scan.pids_count')}"
        row.set_subtitle(GLib.markup_escape_text(subtitle))
        row.set_subtitle_lines(0)
        sid = int(scan["id"])
        if self._scan_select_mode:
            chk = Gtk.CheckButton()
            chk.set_active(sid in self._scan_selected_ids)
            chk.set_valign(Gtk.Align.CENTER)
            chk.connect("toggled", lambda c, s=sid: self._on_scan_checkbox_toggled(s, c.get_active()))
            row.add_prefix(chk)
            row.set_activatable(True)
            row.connect("activated", lambda _r, c=chk: c.set_active(not c.get_active()))
        else:
            icon = Gtk.Image.new_from_icon_name("library-symbolic")
            if self._selected_scan_id is not None and int(self._selected_scan_id) == sid:
                icon.add_css_class("success")
            row.add_prefix(icon)
            # DTC badge is only useful when the scan actually has a fault —
            # showing "0" on every clean scan was noisy.
            if dtc:
                badge = Gtk.Label(label=str(dtc))
                badge.add_css_class("error")
                badge.add_css_class("caption")
                badge.set_halign(Gtk.Align.END)
                row.add_suffix(badge)
            row.set_activatable(True)
            row.connect("activated", lambda _r, s=sid: self._select_scan(s))
            lp = Gtk.GestureLongPress()
            lp.connect("pressed", lambda _g, _x, _y, s=sid: self._enter_scan_select_mode(s))
            row.add_controller(lp)
        return row

    def _open_scan_detail(self, scan_id: int) -> None:
        if self.db is None:
            return
        try:
            data = self.db.get_scan_data(scan_id)
            scans = self.db.list_scans_for_car(self._selected_car_id) if self._selected_car_id else []
            scan_meta = next((s for s in scans if int(s["id"]) == scan_id), None)
            # Previous scan for trend context
            idx = next((i for i, s in enumerate(scans) if int(s["id"]) == scan_id), None)
            prev_meta = scans[idx + 1] if idx is not None and idx + 1 < len(scans) else None
        except Exception:
            log.exception("Could not open scan detail id=%s", scan_id)
            return
        if scan_meta is None:
            return

        try:
            self.db.mark_scan_seen(scan_id)
        except Exception:
            log.exception("Could not mark scan seen id=%s", scan_id)

        page_content = _build_scan_detail_widget(self.language, scan_meta, prev_meta, data)
        ts = self._parse_ts(scan_meta["scanned_at"])
        title = _translate(self.language, "cars.scan.title",
                           date=ts.strftime("%d.%m.%Y %H:%M") if ts else str(scan_id))

        self._set_trash(lambda: self._confirm_delete_scan(scan_id))

        page = Adw.NavigationPage(
            child=self._wrap_sub_page(
                page_content,
                title,
                on_share=(lambda: self._share_scan(scan_id)) if self._is_sync_active() else None,
            ),
            title=title,
        )
        page.set_tag(f"scan-{scan_id}")
        self._scan_detail_page = page
        self._scan_detail_pushed = True
        self._scan_id_shown = scan_id
        self.nav_view.push(page)

    def _enter_scan_select_mode(self, scan_id: int) -> None:
        self._scan_select_mode = True
        self._scan_selected_ids = {scan_id}
        self._render_detail()
        self._update_list_select_buttons()
        self._update_merge_btn_visibility()
        GLib.idle_add(self._reapply_list_select_ui)

    def _exit_scan_select_mode(self) -> None:
        self._scan_select_mode = False
        self._scan_selected_ids = set()
        self._render_detail()
        self._update_list_select_buttons()
        self._update_trash_default()
        self._update_merge_btn_visibility()

    def _on_scan_checkbox_toggled(self, scan_id: int, active: bool) -> None:
        if active:
            self._scan_selected_ids.add(scan_id)
        else:
            self._scan_selected_ids.discard(scan_id)
        if not self._scan_selected_ids:
            self._exit_scan_select_mode()
            return
        self._update_merge_btn_visibility()

    def _on_merge_selected_scans_clicked(self) -> None:
        ids = sorted(self._scan_selected_ids)
        if len(ids) < 2 or self.db is None:
            return
        dialog = Adw.AlertDialog()
        dialog.set_heading(_translate(self.language, "cars.scans.merge.confirm.heading"))
        dialog.set_body(_translate(self.language, "cars.scans.merge.confirm.body", n=len(ids)))
        dialog.add_response("cancel", _translate(self.language, "cars.scans.merge.confirm.cancel"))
        dialog.add_response("merge", _translate(self.language, "cars.scans.merge.confirm.ok"))
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("merge", Adw.ResponseAppearance.SUGGESTED)

        def _on_response(_d: Adw.AlertDialog, response: str) -> None:
            if response != "merge":
                return
            self._do_merge_selected_scans(ids)

        dialog.connect("response", _on_response)
        dialog.present(self)

    def _do_merge_selected_scans(self, ids: list[int]) -> None:
        if self.db is None:
            return
        try:
            survivor = self.db.merge_scans(ids)
        except ValueError as exc:
            code = str(exc)
            key = {
                "too_few":       "cars.scans.merge.error.too_few",
                "gap_too_large": "cars.scans.merge.error.gap",
            }.get(code, "cars.scans.merge.error.generic")
            self._show_toast(_translate(self.language, key))
            return
        except Exception:
            log.exception("Could not merge scans %s", ids)
            self._show_toast(_translate(self.language, "cars.scans.merge.error.generic"))
            return
        self._show_toast(_translate(self.language, "cars.scans.merge.toast_ok"))
        # The survivor stays — make it the selected scan so the chart
        # immediately reflects the merged trace.
        self._selected_scan_id = survivor
        self._scan_pid_stats = {}
        self._exit_scan_select_mode()

    def _confirm_delete_selected_scans(self) -> None:
        n = len(self._scan_selected_ids)
        if n == 0:
            return
        dialog = self._make_delete_dialog("cars.scan.delete_title", "cars.scan.delete_title")
        dialog.set_body(_translate(self.language, "cars.trip.delete_multi_body", n=n))
        dialog.connect("response", lambda _d, r: self._delete_selected_scans() if r == "delete" else None)
        dialog.present(self)

    def _delete_selected_scans(self) -> None:
        if self.db is None:
            return
        for sid in list(self._scan_selected_ids):
            try:
                self.db.delete_scan(sid)
            except Exception:
                log.exception("Could not delete selected scan id=%s", sid)
        self._exit_scan_select_mode()
