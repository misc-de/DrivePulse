"""Autos-Browser: Liste bekannter Fahrzeuge → Detail mit kategorisierten Werten."""
from __future__ import annotations

import time
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .common import SOURCE_LANGUAGE, _normalize_language, _translate
from .db import DriveDB
from .cars_actions import CarsActionsMixin
from .cars_detail_render import CarsDetailRenderMixin
from .cars_layout import CarsLayoutMixin
from .cars_trips import CarsTripsMixin
from .cars_metadata import (
    CATEGORIES,
    _extract_inner_string,
    _wmi_to_brand,
)
from .cars_profiles import _load_profiles
from .cars_accel_runs import CarsAccelRunsMixin
from .cars_scans import CarsScansMixin



# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------


class CarsPage(
    CarsActionsMixin,
    CarsLayoutMixin,
    CarsDetailRenderMixin,
    CarsTripsMixin,
    CarsScansMixin,
    CarsAccelRunsMixin,
    Gtk.Box,
):
    """Zweistufige Navigation: Fahrzeug-Liste → Werte-Detail."""

    __gtype_name__ = "CarsPage"

    LIVE_ID = "__live__"
    LIVE_DETAIL_RENDER_INTERVAL_S = 0.25

    def __init__(self, language: str = SOURCE_LANGUAGE, db: DriveDB | None = None, sidebar_side: str = "left") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.language = _normalize_language(language)
        self.db = db
        self._sidebar_side: str = sidebar_side
        self._latest_live: dict[str, Any] = {}
        self._live_identity: dict[str, str] = {}
        self._obd_connected = False
        self._profiles: list[dict[str, Any]] = []
        self._selected_source: str = self.LIVE_ID
        self._selected_car_id: int | None = None
        self._selected_category: str = CATEGORIES[0][0]
        self._detail_pushed = False
        self.set_header_trash_fn: Any = None
        self._trip_detail_pushed = False
        self._trip_detail_page: Adw.NavigationPage | None = None
        self._scan_detail_pushed = False
        self._scan_detail_page: Adw.NavigationPage | None = None
        self._scan_id_shown: int | None = None
        self._accel_run_detail_page: Adw.NavigationPage | None = None
        self._live_row: Adw.ActionRow | None = None
        self._last_live_detail_render = -self.LIVE_DETAIL_RENDER_INTERVAL_S
        self._narrow = False
        self._cat_rows: list[Gtk.ListBoxRow] = []
        self._trip_select_mode: bool = False
        self._trip_selected_ids: set[int] = set()
        # Wird vom DashboardWindow gesetzt: Callback, wenn der Anwender auf der
        # Wurzel (Auto-Liste) nach rechts wischt, um zum vorherigen Tab zurückzukehren.
        self.on_back_swipe: Callable[[], None] | None = None
        self.on_forward_swipe: Callable[[], None] | None = None
        self._drag_claimed = False

        self.nav_view = Adw.NavigationView()
        self.nav_view.set_hexpand(True)
        self.nav_view.set_vexpand(True)
        self.nav_view.connect("popped", self._on_popped)
        self.append(self.nav_view)

        self._build_list_page()
        self._build_detail_page()
        self.refresh_profiles()

        # Horizontaler Drag in CAPTURE-Phase: greift Wisch-Gesten ab, bevor
        # Adw.NavigationView sie zu fassen bekommt. So funktioniert „nach rechts
        # zurück zum vorherigen Tab" auch auf der Auto-Liste, wo Adw selbst
        # nichts poppen würde.
        drag = Gtk.GestureDrag()
        drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

    # ---------------------------------------------------- Wisch-Gesten

    def _on_drag_begin(self, _gesture: Gtk.GestureDrag, _x: float, _y: float) -> None:
        self._drag_claimed = False

    def _on_drag_update(self, gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        if self._drag_claimed:
            return
        dist_sq = offset_x * offset_x + offset_y * offset_y
        if dist_sq < 64:  # weniger als 8 px — Richtung noch unbekannt
            return
        # Eindeutig vertikal → ablehnen, damit ScrolledWindow-Kinder scrollen können
        if abs(offset_y) > abs(offset_x) * 1.5:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        # Wenn Detail offen ist: Adw.NavigationView soll selbst zurückpoppen.
        if self._detail_pushed:
            return
        # Eindeutig horizontal (mind. 20 px, klar dominanter X-Anteil)
        if abs(offset_x) > 20:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self._drag_claimed = True

    def _on_drag_end(self, _gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        if not self._drag_claimed:
            return
        self._drag_claimed = False
        if offset_x > 60 and self.on_back_swipe is not None:
            self.on_back_swipe()
        elif offset_x < -60 and self.on_forward_swipe is not None:
            self.on_forward_swipe()

    def set_language(self, language: str) -> None:
        self.language = _normalize_language(language)
        self._list_page.set_title(_translate(self.language, "nav.cars"))
        self._categories_label.set_text(_translate(self.language, "cars.categories"))
        self._detail_back_btn.set_tooltip_text(_translate(self.language, "cars.back"))
        self._refresh_list_texts()
        self._rebuild_list()
        for row in self._cat_rows:
            key = getattr(row, "cat_label_key", None)
            lbl = getattr(row, "cat_label_widget", None)
            if key and lbl:
                translated = _translate(self.language, key)
                lbl.set_text(translated)
                row.set_tooltip_text(translated)
        if self._detail_pushed:
            self._render_detail()

    def refresh_profiles(self) -> None:
        self._profiles = _load_profiles(self.db)
        self._rebuild_list()

    def update_live(self, payload: dict[str, Any]) -> None:
        if not payload:
            return
        source = payload.get("source", "")
        if source in ("obd", "mock"):
            for k, v in payload.items():
                if k.startswith("_") or k in ("source", "timestamp", "connection_status", "mock_reason"):
                    continue
                self._latest_live[k] = v
            self._obd_connected = source == "obd"
            self._update_live_row_subtitle()
        if self._selected_source == self.LIVE_ID and self._detail_pushed and self._live_detail_render_due():
            self._render_detail()

    def set_live_identity(self, identity: dict[str, str]) -> None:
        self._live_identity = dict(identity)
        self._update_live_row_subtitle()
        if self._selected_source == self.LIVE_ID and self._detail_pushed:
            self._last_live_detail_render = time.monotonic()
            self._render_detail()

    def _live_detail_render_due(self) -> bool:
        now = time.monotonic()
        if now - self._last_live_detail_render < self.LIVE_DETAIL_RENDER_INTERVAL_S:
            return False
        self._last_live_detail_render = now
        return True

    # ---------------------------------------------------- Listen-Render

    def _rebuild_list(self) -> None:
        while True:
            child = self._list_box.get_first_child()
            if child is None:
                break
            self._list_box.remove(child)

        # Live-Zeile immer oben
        self._live_row = Adw.ActionRow()
        self._live_row.set_title(_translate(self.language, "cars.live.title"))
        self._live_row.set_activatable(True)
        live_icon = Gtk.Image.new_from_icon_name("network-wireless-symbolic")
        self._live_row.add_prefix(live_icon)
        chevron = Gtk.Image.new_from_icon_name("go-next-symbolic")
        self._live_row.add_suffix(chevron)
        self._live_row.connect("activated", lambda _r: self._open_detail(self.LIVE_ID))
        self._list_box.append(self._live_row)
        self._update_live_row_subtitle()

        for entry in self._profiles:
            row = Adw.ActionRow()
            vin = entry.get("vin", "")
            label = entry.get("label") or ""
            brand = entry.get("brand") or ""
            title = label or brand or (f"VIN …{vin[-5:]}" if vin else _translate(self.language, "cars.unknown"))
            row.set_title(GLib.markup_escape_text(title))
            sub_parts: list[str] = []
            if vin:
                sub_parts.append(f"VIN …{vin[-5:]}")
            cal = _extract_inner_string((entry["data"].get("vehicle_info") or {}).get("CALIBRATION_ID"))
            if cal:
                sub_parts.append(f"Cal {cal}")
            if entry.get("scan_label"):
                sub_parts.append(entry["scan_label"])
            row.set_subtitle(GLib.markup_escape_text(" · ".join(sub_parts)) if sub_parts else "—")
            row.set_activatable(True)
            icon = Gtk.Image.new_from_icon_name("preferences-system-symbolic")
            row.add_prefix(icon)
            chev = Gtk.Image.new_from_icon_name("go-next-symbolic")
            row.add_suffix(chev)
            row.connect("activated", lambda _r, p=str(entry["path"]): self._open_detail(p))
            self._list_box.append(row)

        self._empty_label.set_visible(not self._profiles)

    def _update_live_row_subtitle(self) -> None:
        row = self._live_row
        if row is None:
            return
        vin = _extract_inner_string(self._live_identity.get("VIN"))
        if vin:
            brand = _wmi_to_brand(vin)
            sub = f"{brand} · …{vin[-5:]}" if brand else f"…{vin[-5:]}"
        elif self._obd_connected:
            sub = _translate(self.language, "cars.live.connected")
        else:
            sub = _translate(self.language, "cars.live.subtitle")
        row.set_subtitle(GLib.markup_escape_text(sub))

    # ---------------------------------------------------- Detail-Navigation

    _LIVE_HIDDEN_CATS = frozenset({"trips", "acceleration_runs", "scans"})

    def _update_category_visibility(self, is_live: bool) -> None:
        for row in self._cat_rows:
            cat_key = getattr(row, "cat_key", "")
            row.set_visible(not (is_live and cat_key in self._LIVE_HIDDEN_CATS))
        if is_live and self._selected_category in self._LIVE_HIDDEN_CATS:
            self._selected_category = "vehicle"
            for row in self._cat_rows:
                if getattr(row, "cat_key", "") == "vehicle":
                    self.category_list.select_row(row)
                    break

    def _open_detail(self, source: str) -> None:
        self._selected_source = source
        self._selected_car_id = None
        if source == self.LIVE_ID:
            title = _translate(self.language, "cars.live.title")
        else:
            entry = next((e for e in self._profiles if str(e.get("path")) == source), None)
            if entry:
                vin = entry.get("vin", "")
                label = entry.get("label") or ""
                brand = entry.get("brand") or ""
                base = label or brand or _translate(self.language, "cars.unknown")
                title = base if (label or not vin) else f"{base} · …{vin[-5:]}"
                self._selected_car_id = entry.get("car_id")
            else:
                title = _translate(self.language, "cars.unknown")
        self._detail_page.set_title(title)
        self._detail_title.set_text(title)
        is_real_car = source != self.LIVE_ID and self._selected_car_id is not None
        # Show trash only for real vehicles, not the live view
        if is_real_car:
            self._set_trash(self._confirm_delete_vehicle)
        else:
            self._set_trash(None)
        self._rename_btn.set_visible(is_real_car)
        self._update_category_visibility(source == self.LIVE_ID)
        self._render_detail()
        if not self._detail_pushed:
            self.nav_view.push(self._detail_page)
            self._detail_pushed = True

    def _on_popped(self, _view: Adw.NavigationView, page: Adw.NavigationPage) -> None:
        if page is self._detail_page:
            self._detail_pushed = False
            self._trip_select_mode = False
            self._trip_selected_ids = set()
            self._select_revealer.set_reveal_child(False)
            self._set_trash(None)
            self._rename_btn.set_visible(False)
        if page is self._trip_detail_page:
            self._trip_detail_pushed = False
            self._trip_detail_page = None
            if self._detail_pushed and self._selected_category == "trips":
                self._render_detail()
            # Restore vehicle delete action when returning to vehicle detail
            if self._detail_pushed and self._selected_car_id is not None:
                self._set_trash(self._confirm_delete_vehicle)
        if page is self._scan_detail_page:
            self._scan_detail_pushed = False
            self._scan_detail_page = None
            self._scan_id_shown = None
            if self._detail_pushed and self._selected_category == "scans":
                self._render_detail()
            if self._detail_pushed and self._selected_car_id is not None:
                self._set_trash(self._confirm_delete_vehicle)
        if page is self._accel_run_detail_page:
            self._accel_run_detail_page = None
            if self._detail_pushed and self._selected_category == "acceleration_runs":
                self._render_detail()
            if self._detail_pushed and self._selected_car_id is not None:
                self._set_trash(self._confirm_delete_vehicle)

    def _set_trash(self, action_fn: Any) -> None:
        if self.set_header_trash_fn is not None:
            self.set_header_trash_fn(action_fn)

    def _on_category_selected(self, _box: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return
        new_cat = getattr(row, "cat_key", CATEGORIES[0][0])
        if self._trip_select_mode and new_cat != "trips":
            self._trip_select_mode = False
            self._trip_selected_ids = set()
            self._select_revealer.set_reveal_child(False)
        self._selected_category = new_cat
        if self._detail_pushed:
            self._render_detail()
