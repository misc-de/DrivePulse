"""Rename and delete actions for CarsPage."""
from __future__ import annotations

from pathlib import Path

from gi.repository import Adw, GLib, Gtk

from .common import _translate
from .diagnostics import get_logger


log = get_logger(__name__)


class CarsActionsMixin:
    def _make_delete_dialog(self, heading_key: str, body_key: str) -> Adw.AlertDialog:
        """Create a destructive AlertDialog with a red heading."""
        try:
            dark = Adw.StyleManager.get_default().get_dark()
        except Exception:
            dark = True
        color = "#ff7b63" if dark else "#e01b24"
        heading = _translate(self.language, heading_key)
        body = _translate(self.language, body_key)
        dialog = Adw.AlertDialog()
        dialog.set_heading_use_markup(True)
        dialog.set_heading(f'<span foreground="{color}"><b>{GLib.markup_escape_text(heading)}</b></span>')
        dialog.set_body(body)
        dialog.add_response("cancel", _translate(self.language, "cars.trip.delete_cancel"))
        dialog.add_response("delete", _translate(self.language, "cars.trip.delete_confirm"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        return dialog

    def _confirm_delete_trip(self, trip_id: int) -> None:
        dialog = self._make_delete_dialog("cars.trip.delete_title", "cars.trip.delete_body")
        dialog.connect("response", lambda _d, resp: self._delete_trip(trip_id) if resp == "delete" else None)
        dialog.present(self)

    def _delete_trip(self, trip_id: int) -> None:
        if self.db is None:
            return
        try:
            self.db.delete_trip(trip_id)
        except Exception:
            log.exception("Could not delete trip id=%s", trip_id)
            return
        if self._trip_detail_page is not None:
            self.nav_view.pop()

    # ---------------------------------------------------- Scan löschen

    def _confirm_delete_scan(self, scan_id: int) -> None:
        dialog = self._make_delete_dialog("cars.scan.delete_title", "cars.scan.delete_body")
        dialog.connect("response", lambda _d, r: self._delete_scan(scan_id) if r == "delete" else None)
        dialog.present(self)

    def _delete_scan(self, scan_id: int) -> None:
        if self.db is None:
            return
        try:
            self.db.delete_scan(scan_id)
        except Exception:
            log.exception("Could not delete scan id=%s", scan_id)
            return
        if self._scan_detail_page is not None:
            self.nav_view.pop()
        self._scan_id_shown = None
        self._render_detail()

    # ---------------------------------------------------- Fahrzeug löschen

    def _confirm_add_live_vehicle(self) -> None:
        vin = self._live_vin()
        if not vin:
            return
        dialog = Adw.AlertDialog(
            heading=_translate(self.language, "cars.live.add.title"),
            body=_translate(self.language, "cars.live.add.body", vin=vin),
        )
        dialog.add_response("cancel", _translate(self.language, "cars.live.add.cancel"))
        dialog.add_response("add", _translate(self.language, "cars.live.add.confirm"))
        dialog.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("add")
        dialog.set_close_response("cancel")

        def _on_response(_d: Adw.AlertDialog, resp: str) -> None:
            if resp != "add":
                return
            self._add_live_vehicle()

        dialog.connect("response", _on_response)
        dialog.present(self)

    def _add_live_vehicle(self) -> None:
        if not self._live_vin():
            return
        car_id: int | None = None
        if self.on_live_vehicle_add is not None:
            try:
                car_id = self.on_live_vehicle_add(dict(self._live_identity))
            except Exception:
                log.exception("Could not add live vehicle through callback")
                return
        elif self.db is not None:
            try:
                car_id = self.db.upsert_car(
                    vin=self._live_vin(),
                    brand=self._live_identity.get("brand"),
                    cal_id=self._live_identity.get("CALIBRATION_ID"),
                    cvn=self._live_identity.get("CVN"),
                    protocol=self._live_identity.get("protocol"),
                    profile_path=self._live_identity.get("profile_path"),
                )
            except Exception:
                log.exception("Could not add live vehicle")
                return
        if car_id is None:
            return
        self.refresh_profiles()
        self._selected_car_id = car_id
        self._update_live_add_button()

    def _open_rename_dialog(self) -> None:
        car_id = self._selected_car_id
        if car_id is None:
            return
        entry_widget = Gtk.Entry()
        entry_widget.set_hexpand(True)
        entry_widget.set_margin_top(8)
        current_label = self._detail_title.get_text()
        entry_widget.set_text(current_label)
        entry_widget.set_placeholder_text(_translate(self.language, "cars.vehicle.rename_placeholder"))
        entry_widget.select_region(0, -1)

        dialog = Adw.AlertDialog(
            heading=_translate(self.language, "cars.vehicle.rename_title"),
        )
        dialog.set_extra_child(entry_widget)
        dialog.add_response("cancel", _translate(self.language, "cars.vehicle.rename_cancel"))
        dialog.add_response("save", _translate(self.language, "cars.vehicle.rename_confirm"))
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        entry_widget.connect(
            "activate",
            lambda _e: dialog.response("save"),
        )

        def _on_response(_d: Adw.AlertDialog, resp: str) -> None:
            if resp != "save":
                return
            new_name = entry_widget.get_text().strip()
            if self.db is None:
                return
            try:
                self.db.rename_car(car_id, new_name)
            except Exception:
                log.exception("Could not rename car id=%s", car_id)
                return
            # Update entry in profile list so the title stays current
            for e in self._profiles:
                if e.get("car_id") == car_id:
                    e["label"] = new_name
            # Rebuild display title the same way _open_detail does
            entry = next((e for e in self._profiles if e.get("car_id") == car_id), None)
            if entry:
                vin = entry.get("vin", "")
                label = new_name
                brand = entry.get("brand") or ""
                base = label or brand or _translate(self.language, "cars.unknown")
                title = base if (label or not vin) else f"{base} · …{vin[-5:]}"
            else:
                title = new_name or _translate(self.language, "cars.unknown")
            self._detail_title.set_text(title)
            self._detail_page.set_title(title)
            GLib.idle_add(self._rebuild_list)

        dialog.connect("response", _on_response)
        dialog.present(self)

    def _confirm_delete_vehicle(self) -> None:
        dialog = self._make_delete_dialog("cars.vehicle.delete_title", "cars.vehicle.delete_body")
        dialog.connect("response", lambda _d, r: self._delete_vehicle() if r == "delete" else None)
        dialog.present(self)

    def _delete_vehicle(self) -> None:
        if self.db and self._selected_car_id:
            try:
                self.db.delete_car(self._selected_car_id)
            except Exception:
                log.exception("Could not delete car id=%s", self._selected_car_id)
        entry = next(
            (e for e in self._profiles if e.get("path") and str(e["path"]) == self._selected_source),
            None,
        )
        if entry and entry.get("path"):
            try:
                Path(entry["path"]).unlink(missing_ok=True)
            except Exception:
                log.exception("Could not delete profile file %s", entry["path"])
        if self._detail_pushed:
            self.nav_view.pop()
