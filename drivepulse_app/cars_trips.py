"""Trip list, selection and detail helpers for CarsPage."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from gi.repository import Adw, GLib, Gtk

from .common import _translate
from .cars_trip_widgets import _build_trip_detail_widget
from .diagnostics import get_logger


log = get_logger(__name__)


class CarsTripsMixin:
    def _render_trips_into_value_list(self) -> None:
        if self.db is None or self._selected_car_id is None:
            self.value_list.append(self._info_row(_translate(self.language, "cars.trips.empty")))
            return
        try:
            trips = self.db.list_trips_for_car(self._selected_car_id)
        except Exception:
            log.exception("Could not list trips for car id=%s", self._selected_car_id)
            trips = []
        if not trips:
            self.value_list.append(self._info_row(_translate(self.language, "cars.trips.empty")))
            return
        for trip in trips:
            self.value_list.append(self._make_trip_row(trip))

    def _info_row(self, text: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        lbl = Gtk.Label(label=text, xalign=0.0)
        lbl.add_css_class("dim-label")
        lbl.set_wrap(True)
        lbl.set_margin_top(10)
        lbl.set_margin_bottom(10)
        lbl.set_margin_start(14)
        lbl.set_margin_end(14)
        row.set_child(lbl)
        return row

    def _make_trip_row(self, trip: Any) -> Adw.ActionRow:
        row = Adw.ActionRow()
        trip_id = int(trip["id"])
        row.set_title(GLib.markup_escape_text(self._trip_display_title(trip)))

        keys = trip.keys() if hasattr(trip, "keys") else []
        shared_at = trip["shared_at"] if "shared_at" in keys else None
        seen_at = trip["seen_at"] if "seen_at" in keys else None
        if shared_at and not seen_at:
            dot = Gtk.Label(label="●")
            dot.add_css_class("accent")
            dot.set_valign(Gtk.Align.CENTER)
            row.add_prefix(dot)

        parts: list[str] = []
        dur = trip["duration_s"]
        if dur:
            mins = int(dur // 60)
            secs = int(dur % 60)
            parts.append(f"{mins} min {secs:02d} s" if mins else f"{secs} s")
        km = trip["distance_km"]
        if km is not None:
            parts.append(f"{km:.1f} km")
        vmax = trip["max_speed_kmh"]
        if vmax is not None:
            parts.append(f"max {vmax:.0f} km/h")
        if trip["ended_at"] is None:
            parts.append(f"⏺ {_translate(self.language, 'cars.trip.ongoing')}")
        row.set_subtitle(GLib.markup_escape_text(" · ".join(parts)))
        row.set_subtitle_lines(0)

        if self._trip_select_mode:
            chk = Gtk.CheckButton()
            chk.set_active(trip_id in self._trip_selected_ids)
            chk.set_valign(Gtk.Align.CENTER)
            chk.connect("toggled", lambda c, tid=trip_id: self._on_trip_checkbox_toggled(tid, c.get_active()))
            row.add_prefix(chk)
            row.set_activatable(False)
        else:
            icon = Gtk.Image.new_from_icon_name("mark-location-symbolic")
            row.add_prefix(icon)
            chev = Gtk.Image.new_from_icon_name("go-next-symbolic")
            row.add_suffix(chev)
            row.set_activatable(True)
            row.connect("activated", lambda _r, tid=trip_id: self._open_trip_detail(tid))
            lp = Gtk.GestureLongPress()
            lp.connect("pressed", lambda _g, _x, _y, tid=trip_id: self._enter_trip_select_mode(tid))
            row.add_controller(lp)

        return row

    def _parse_ts(self, raw: Any) -> datetime | None:
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except Exception:
            return None

    # ---------------------------------------------------- Fahrt-Detail-Page

    def _open_trip_detail(self, trip_id: int) -> None:
        if self.db is None:
            return
        try:
            samples = list(self.db.samples_for_trip(trip_id))
            trips = self.db.list_trips_for_car(self._selected_car_id) if self._selected_car_id else []
            trip = next((t for t in trips if int(t["id"]) == trip_id), None)
        except Exception:
            log.exception("Could not open trip detail for trip id=%s", trip_id)
            samples, trip = [], None
        if trip is None:
            return
        try:
            self.db.mark_trip_seen(trip_id)
        except Exception:
            log.exception("Could not mark trip seen id=%s", trip_id)

        page_content = _build_trip_detail_widget(self.language, trip, samples)
        title = self._trip_detail_title(trip)

        page_ref: list[Adw.NavigationPage | None] = [None]

        def _on_rename(title_lbl: Gtk.Label) -> None:
            self._open_trip_rename_dialog(trip_id, title_lbl, page_ref)

        # Desktop (split view uncollapsed): show the trip detail inline inside
        # the same value-scroll area where the list lives, instead of pushing
        # a sub-page that hides the trips list on the other side.
        if not self._split_view.get_collapsed():
            inline = self._wrap_sub_page(
                page_content,
                title,
                on_rename=_on_rename,
                on_share=(lambda: self._share_trip(trip_id)) if self._is_sync_active() else None,
                on_delete=lambda: self._confirm_delete_trip(trip_id),
                on_back=lambda: self._render_detail(),
            )
            self._value_scroll.set_child(inline)
            return

        page = Adw.NavigationPage(
            child=self._wrap_sub_page(
                page_content,
                title,
                on_rename=_on_rename,
                on_share=(lambda: self._share_trip(trip_id)) if self._is_sync_active() else None,
                on_delete=lambda: self._confirm_delete_trip(trip_id),
            ),
            title=title,
        )
        page.set_tag(f"trip-{trip_id}")
        page_ref[0] = page
        self._trip_detail_page = page
        self._trip_detail_pushed = True
        self.nav_view.push(page)

    def _open_trip_rename_dialog(
        self,
        trip_id: int,
        title_lbl: Gtk.Label,
        page_ref: list,
    ) -> None:
        entry = Gtk.Entry()
        entry.set_hexpand(True)
        entry.set_margin_top(8)
        entry.set_text(title_lbl.get_label())
        entry.set_placeholder_text(_translate(self.language, "cars.trip.rename_placeholder"))
        entry.select_region(0, -1)

        dialog = Adw.AlertDialog(
            heading=_translate(self.language, "cars.trip.rename_title"),
        )
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", _translate(self.language, "cars.trip.rename_cancel"))
        dialog.add_response("save", _translate(self.language, "cars.trip.rename_confirm"))
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")
        entry.connect("activate", lambda _e: dialog.response("save"))

        def _on_response(_d: Adw.AlertDialog, resp: str) -> None:
            if resp != "save":
                return
            new_name = entry.get_text().strip()
            if self.db is None:
                return
            try:
                self.db.rename_trip(trip_id, new_name)
            except Exception:
                log.exception("Could not rename trip id=%s", trip_id)
                return
            title_lbl.set_label(new_name if new_name else title_lbl.get_label())
            if page_ref[0] is not None and new_name:
                page_ref[0].set_title(new_name)
            GLib.idle_add(self._render_detail)

        dialog.connect("response", _on_response)
        dialog.present(self)

    # ---------------------------------------------------- Fahrten Multi-Auswahl

    def _enter_trip_select_mode(self, trip_id: int) -> None:
        self._trip_select_mode = True
        self._trip_selected_ids = {trip_id}
        self._render_detail()
        self._set_trash(lambda: self._confirm_delete_selected_trips())

    def _exit_trip_select_mode(self) -> None:
        self._trip_select_mode = False
        self._trip_selected_ids = set()
        self._render_detail()
        if self._selected_car_id is not None:
            self._set_trash(self._confirm_delete_vehicle)
        else:
            self._set_trash(None)

    def _on_trip_checkbox_toggled(self, trip_id: int, active: bool) -> None:
        if active:
            self._trip_selected_ids.add(trip_id)
        else:
            self._trip_selected_ids.discard(trip_id)
        if not self._trip_selected_ids:
            self._exit_trip_select_mode()

    def _confirm_delete_selected_trips(self) -> None:
        n = len(self._trip_selected_ids)
        if n == 0:
            return
        dialog = self._make_delete_dialog("cars.trip.delete_title", "cars.trip.delete_title")
        # Override body with dynamic count text
        dialog.set_body(_translate(self.language, "cars.trip.delete_multi_body", n=n))
        dialog.connect("response", lambda _d, r: self._delete_selected_trips() if r == "delete" else None)
        dialog.present(self)

    def _delete_selected_trips(self) -> None:
        if self.db is None:
            return
        for tid in list(self._trip_selected_ids):
            try:
                self.db.delete_trip(tid)
            except Exception:
                log.exception("Could not delete selected trip id=%s", tid)
        self._exit_trip_select_mode()

    def _trip_display_title(self, trip: Any) -> str:
        """Label if set, otherwise formatted start date."""
        label = trip["label"] if "label" in trip.keys() else None
        if label:
            return label
        started = self._parse_ts(trip["started_at"])
        if started is None:
            return _translate(self.language, "cars.trip.title", id=int(trip["id"]))
        return started.strftime("%d.%m.%Y · %H:%M")

    def _trip_detail_title(self, trip: Any) -> str:
        return self._trip_display_title(trip)

    # ---------------------------------------------------- Scan-Liste & Detail
