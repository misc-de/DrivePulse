"""Autos-Browser: Liste bekannter Fahrzeuge → Detail mit kategorisierten Werten."""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from .common import SOURCE_LANGUAGE, _normalize_language, _translate
from .db import DriveDB
from .cars_metadata import (
    CATEGORIES,
    LIVE_KEY_TO_PID,
    _SPECIAL_ADAPTER_V,
    _SPECIAL_CAL,
    _SPECIAL_CVN,
    _SPECIAL_DTC,
    _SPECIAL_PENDING,
    _SPECIAL_PROTO,
    _SPECIAL_SCAN_DATE,
    _SPECIAL_VIN,
    _extract_inner_string,
    _format_value_unit,
    _parse_profile_pid_key,
    _wmi_to_brand,
)
from .cars_profiles import _load_profiles
from .cars_scan_widgets import _build_scan_detail_widget, _format_scan_date
from .cars_trip_widgets import _build_trip_detail_widget



# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------


class CarsPage(Gtk.Box):
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

    # ---------------------------------------------------- List-Aufbau

    def _build_list_page(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_margin_top(16)
        outer.set_margin_bottom(16)
        outer.set_margin_start(16)
        outer.set_margin_end(16)

        self._list_intro = Gtk.Label(xalign=0.0)
        self._list_intro.add_css_class("dim-label")
        self._list_intro.set_wrap(True)
        outer.append(self._list_intro)

        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list_box.add_css_class("boxed-list")
        self._list_box.set_valign(Gtk.Align.START)
        outer.append(self._list_box)

        self._empty_label = Gtk.Label(xalign=0.0)
        self._empty_label.add_css_class("dim-label")
        self._empty_label.set_wrap(True)
        self._empty_label.set_visible(False)
        outer.append(self._empty_label)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_hexpand(True)
        scroll.set_child(outer)

        self._list_page = Adw.NavigationPage(
            child=scroll,
            title=_translate(self.language, "nav.cars"),
        )
        self._list_page.set_tag("list")
        self.nav_view.add(self._list_page)
        self._refresh_list_texts()

    def _refresh_list_texts(self) -> None:
        self._list_intro.set_text(_translate(self.language, "cars.list.intro"))
        self._empty_label.set_text(_translate(self.language, "cars.empty"))

    # ---------------------------------------------------- Detail-Aufbau

    def _build_detail_page(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        head.set_margin_start(8)
        head.set_margin_top(8)
        head.set_margin_end(12)
        head.set_margin_bottom(4)
        self._detail_back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        self._detail_back_btn.add_css_class("flat")
        self._detail_back_btn.set_tooltip_text(_translate(self.language, "cars.back"))
        self._detail_back_btn.connect("clicked", lambda _b: self.nav_view.pop())
        self._detail_title = Gtk.Label(xalign=0.0)
        self._detail_title.add_css_class("title-3")
        self._detail_title.set_hexpand(True)

        self._rename_btn = Gtk.Button(icon_name="document-edit-symbolic")
        self._rename_btn.add_css_class("flat")
        self._rename_btn.set_visible(False)
        self._rename_btn.connect("clicked", lambda _b: self._open_rename_dialog())

        head.append(self._detail_back_btn)
        head.append(self._detail_title)
        head.append(self._rename_btn)
        outer.append(head)
        outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        body.set_hexpand(True)
        body.set_vexpand(True)
        self._detail_body = body

        self._sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._sidebar.set_margin_top(12)
        self._sidebar.set_margin_bottom(12)
        self._sidebar.set_margin_start(8)
        self._sidebar.set_margin_end(4)

        self._categories_label = Gtk.Label(xalign=0.0)
        self._categories_label.add_css_class("heading")
        self._sidebar.append(self._categories_label)

        self.category_list = Gtk.ListBox()
        self.category_list.set_selection_mode(Gtk.SelectionMode.BROWSE)
        self.category_list.add_css_class("navigation-sidebar")
        self.category_list.connect("row-selected", self._on_category_selected)
        for cat_key, cat_name_key, icon_name, _items in CATEGORIES:
            row = Gtk.ListBoxRow()
            row.cat_key = cat_key  # type: ignore[attr-defined]
            row.cat_label_key = cat_name_key  # type: ignore[attr-defined]

            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            hbox.set_margin_top(8)
            hbox.set_margin_bottom(8)
            hbox.set_margin_start(8)
            hbox.set_margin_end(8)

            icon = Gtk.Image.new_from_icon_name(icon_name)
            icon.set_pixel_size(18)
            hbox.append(icon)

            lbl = Gtk.Label(label=_translate(self.language, cat_name_key), xalign=0.0)
            lbl.set_hexpand(True)
            hbox.append(lbl)

            row.cat_label_widget = lbl  # type: ignore[attr-defined]
            row.cat_icon_widget = icon  # type: ignore[attr-defined]
            row.cat_hbox = hbox  # type: ignore[attr-defined]
            row.set_tooltip_text(_translate(self.language, cat_name_key))

            row.set_child(hbox)
            self.category_list.append(row)
            self._cat_rows.append(row)

        cat_scroll = Gtk.ScrolledWindow()
        cat_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        cat_scroll.set_vexpand(True)
        cat_scroll.set_child(self.category_list)
        self._sidebar.append(cat_scroll)

        self._sidebar_separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)

        self._apply_narrow_to_sidebar()

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        content.set_hexpand(True)
        content.set_vexpand(True)
        self._detail_content = content

        self.content_title = Gtk.Label(xalign=0.0)
        self.content_title.add_css_class("title-2")
        self.content_title.set_margin_top(12)
        self.content_title.set_margin_start(16)
        content.append(self.content_title)

        self.content_subtitle = Gtk.Label(xalign=0.0)
        self.content_subtitle.add_css_class("dim-label")
        self.content_subtitle.set_margin_start(16)
        self.content_subtitle.set_margin_bottom(8)
        content.append(self.content_subtitle)

        self.value_list = Gtk.ListBox()
        self.value_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.value_list.add_css_class("boxed-list")
        self.value_list.set_margin_start(16)
        self.value_list.set_margin_end(16)
        self.value_list.set_margin_bottom(16)
        self.value_list.set_valign(Gtk.Align.START)

        value_scroll = Gtk.ScrolledWindow()
        value_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        value_scroll.set_vexpand(True)
        value_scroll.set_hexpand(True)
        value_scroll.set_child(self.value_list)
        content.append(value_scroll)

        # Selection action bar (trips multi-select mode)
        self._select_count_lbl = Gtk.Label(xalign=0.0)
        self._select_count_lbl.set_hexpand(True)

        self._select_delete_btn = Gtk.Button()
        self._select_delete_btn.add_css_class("destructive-action")
        self._select_delete_btn.connect("clicked", lambda _b: self._confirm_delete_selected_trips())

        _sel_cancel_btn = Gtk.Button(label="")
        _sel_cancel_btn.add_css_class("flat")
        _sel_cancel_btn.connect("clicked", lambda _b: self._exit_trip_select_mode())

        self._select_cancel_btn = _sel_cancel_btn

        sel_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sel_bar.set_margin_start(16)
        sel_bar.set_margin_end(16)
        sel_bar.set_margin_top(8)
        sel_bar.set_margin_bottom(8)
        sel_bar.append(self._select_count_lbl)
        sel_bar.append(self._select_delete_btn)
        sel_bar.append(_sel_cancel_btn)

        self._select_revealer = Gtk.Revealer()
        self._select_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self._select_revealer.set_reveal_child(False)
        self._select_revealer.set_child(sel_bar)
        content.append(self._select_revealer)

        # Initiale Anordnung gemäß Einstellung
        self._apply_sidebar_side_to_body()
        outer.append(body)

        self._categories_label.set_text(_translate(self.language, "cars.categories"))

        self._detail_page = Adw.NavigationPage(child=outer, title="")
        self._detail_page.set_tag("detail")

        first_row = self.category_list.get_row_at_index(0)
        if first_row is not None:
            self.category_list.select_row(first_row)

    # ---------------------------------------------------- öffentliche API

    def is_detail_open(self) -> bool:
        """True, solange die Detail-Seite im NavigationView gepusht ist."""
        return self._detail_pushed

    def set_narrow(self, narrow: bool) -> None:
        """Auf Smartphone-Breiten: Labels ausblenden, nur Icons zeigen."""
        if narrow == self._narrow:
            return
        self._narrow = narrow
        self._apply_narrow_to_sidebar()

    def _apply_sidebar_side_to_body(self) -> None:
        """Sidebar links oder rechts vom Content anordnen."""
        body = self._detail_body
        sidebar = self._sidebar
        sep = self._sidebar_separator
        content = self._detail_content
        for w in (sidebar, sep, content):
            if w.get_parent() == body:
                body.remove(w)
        if self._sidebar_side == "right":
            body.append(content)
            body.append(sep)
            body.append(sidebar)
        else:
            body.append(sidebar)
            body.append(sep)
            body.append(content)

    def set_sidebar_side(self, side: str) -> None:
        if side == self._sidebar_side:
            return
        self._sidebar_side = side
        self._apply_sidebar_side_to_body()

    def _apply_narrow_to_sidebar(self) -> None:
        narrow = self._narrow
        # Sidebar-Breite umstellen
        self._sidebar.set_size_request(56 if narrow else 220, -1)
        # Überschrift „Kategorien" ausblenden, wenn schmal
        self._categories_label.set_visible(not narrow)
        for row in self._cat_rows:
            lbl = getattr(row, "cat_label_widget", None)
            hbox = getattr(row, "cat_hbox", None)
            if lbl is not None:
                lbl.set_visible(not narrow)
            if hbox is not None:
                hbox.set_halign(Gtk.Align.CENTER if narrow else Gtk.Align.FILL)

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

    # ---------------------------------------------------- Daten + Detail-Render

    def _current_data(self) -> tuple[dict[str, Any], str]:
        if self._selected_source == self.LIVE_ID:
            d: dict[str, Any] = {}
            for live_key, pid in LIVE_KEY_TO_PID.items():
                if live_key in self._latest_live:
                    d[pid] = self._latest_live[live_key]
            for special_key, identity_key in (
                (_SPECIAL_VIN, "VIN"),
                (_SPECIAL_CAL, "CALIBRATION_ID"),
                (_SPECIAL_CVN, "CVN"),
                (_SPECIAL_PROTO, "protocol"),
            ):
                if self._live_identity.get(identity_key):
                    d[special_key] = self._live_identity[identity_key]
            return d, _translate(self.language, "cars.live.title")
        for entry in self._profiles:
            if str(entry["path"]) == self._selected_source:
                vin = entry.get("vin", "")
                brand = entry.get("brand") or ""
                label = brand if brand else _translate(self.language, "cars.unknown")
                if vin:
                    label = f"{label} · …{vin[-5:]}"
                return self._flatten_profile(entry["data"]), label
        return {}, "—"

    def _flatten_profile(self, data: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for raw_key, raw_val in (data.get("live_data") or {}).items():
            pid = _parse_profile_pid_key(raw_key)
            if pid:
                out[pid] = raw_val
        info = data.get("vehicle_info") or {}
        if info.get("VIN"):
            out[_SPECIAL_VIN] = _extract_inner_string(info["VIN"])
        if info.get("CALIBRATION_ID"):
            out[_SPECIAL_CAL] = _extract_inner_string(info["CALIBRATION_ID"])
        if info.get("CVN"):
            out[_SPECIAL_CVN] = _extract_inner_string(info["CVN"])
        if data.get("protocol"):
            out[_SPECIAL_PROTO] = str(data["protocol"])
        if data.get("scanned_at"):
            out[_SPECIAL_SCAN_DATE] = _format_scan_date(data["scanned_at"])
        dtcs = data.get("dtcs") or []
        none_text = _translate(self.language, "cars.dtc.none")
        out[_SPECIAL_DTC] = none_text if not dtcs else "  ".join(str(d) for d in dtcs)
        pending = data.get("pending_dtcs") or []
        out[_SPECIAL_PENDING] = none_text if not pending else "  ".join(str(d) for d in pending)
        return out

    def _format_entry(self, pid_key: str, raw: Any) -> tuple[str, bool]:
        if pid_key == _SPECIAL_VIN and raw:
            vin = _extract_inner_string(raw)
            brand = _wmi_to_brand(vin)
            return (f"{vin}  ({brand})" if brand else vin, False)
        if pid_key.startswith("__"):
            if raw is None or raw == "":
                return ("—", True)
            return (_extract_inner_string(raw) if isinstance(raw, str) else str(raw), False)
        if raw is None:
            return ("—", True)
        text = _format_value_unit(raw)
        return (text, text == "—")

    def _render_detail(self) -> None:
        while True:
            child = self.value_list.get_first_child()
            if child is None:
                break
            self.value_list.remove(child)

        cat_meta = next((c for c in CATEGORIES if c[0] == self._selected_category), CATEGORIES[0])
        cat_key, cat_name_key, _icon_name, items = cat_meta
        self.content_title.set_text(_translate(self.language, cat_name_key))

        data, source_label = self._current_data()
        self.content_subtitle.set_text("")

        if cat_key == "trips":
            self._render_trips_into_value_list()
            return

        if cat_key == "scans":
            self._render_scans_into_value_list()
            return

        if cat_key == "acceleration_runs":
            self._render_accel_runs_into_value_list()
            return

        for pid_key, label_key in items:
            raw = data.get(pid_key)
            value_text, is_unknown = self._format_entry(pid_key, raw)
            label = _translate(self.language, label_key)
            self.value_list.append(self._make_stacked_row(label, value_text, is_unknown))

    def _make_inline_row(self, pid_key: str, label: str, value_text: str, is_unknown: bool) -> Adw.ActionRow:
        row = Adw.ActionRow()
        row.set_title(GLib.markup_escape_text(label))
        row.set_subtitle(GLib.markup_escape_text(pid_key) if not pid_key.startswith("__") else "")

        value_label = Gtk.Label(label=value_text, xalign=1.0)
        value_label.add_css_class("monospace")
        value_label.set_wrap(True)
        value_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        value_label.set_max_width_chars(28)
        if is_unknown:
            value_label.add_css_class("dim-label")
        row.add_suffix(value_label)
        return row

    # ---------------------------------------------------- Fahrten-Rendering

    def _render_trips_into_value_list(self) -> None:
        if self.db is None or self._selected_car_id is None:
            self.value_list.append(self._info_row(_translate(self.language, "cars.trips.empty")))
            return
        try:
            trips = self.db.list_trips_for_car(self._selected_car_id)
        except Exception:
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
        started = self._parse_ts(trip["started_at"])
        title = started.strftime("%d.%m.%Y · %H:%M") if started else _translate(self.language, "cars.trip.title", id=trip_id)
        row.set_title(GLib.markup_escape_text(title))

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
        n = trip["samples_count"] or 0
        parts.append(f"{n} {_translate(self.language, 'cars.trip.samples')}")
        if trip["ended_at"] is None:
            parts.append(f"⏺ {_translate(self.language, 'cars.trip.ongoing')}")
        row.set_subtitle(GLib.markup_escape_text(" · ".join(parts)))

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
            samples, trip = [], None
        if trip is None:
            return

        page_content = _build_trip_detail_widget(self.language, trip, samples)
        title = self._trip_detail_title(trip)

        self._set_trash(lambda: self._confirm_delete_trip(trip_id))

        page = Adw.NavigationPage(child=page_content, title=title)
        page.set_tag(f"trip-{trip_id}")
        self._trip_detail_page = page
        self._trip_detail_pushed = True
        self.nav_view.push(page)

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
            return
        if self._scan_detail_page is not None:
            self.nav_view.pop()
        self._scan_id_shown = None
        self._render_detail()

    # ---------------------------------------------------- Fahrzeug löschen

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
                pass
        entry = next(
            (e for e in self._profiles if e.get("path") and str(e["path"]) == self._selected_source),
            None,
        )
        if entry and entry.get("path"):
            try:
                Path(entry["path"]).unlink(missing_ok=True)
            except Exception:
                pass
        if self._detail_pushed:
            self.nav_view.pop()
        GLib.idle_add(self.refresh_profiles)

    # ---------------------------------------------------- Fahrten Multi-Auswahl

    def _enter_trip_select_mode(self, trip_id: int) -> None:
        self._trip_select_mode = True
        self._trip_selected_ids = {trip_id}
        self._render_detail()
        self._update_select_bar()
        self._select_revealer.set_reveal_child(True)

    def _exit_trip_select_mode(self) -> None:
        self._trip_select_mode = False
        self._trip_selected_ids = set()
        self._select_revealer.set_reveal_child(False)
        self._render_detail()

    def _on_trip_checkbox_toggled(self, trip_id: int, active: bool) -> None:
        if active:
            self._trip_selected_ids.add(trip_id)
        else:
            self._trip_selected_ids.discard(trip_id)
        self._update_select_bar()

    def _update_select_bar(self) -> None:
        n = len(self._trip_selected_ids)
        self._select_count_lbl.set_text(
            _translate(self.language, "cars.trip.selected_count", n=n)
        )
        self._select_delete_btn.set_label(_translate(self.language, "cars.trip.delete_confirm"))
        self._select_delete_btn.set_sensitive(n > 0)
        self._select_cancel_btn.set_label(_translate(self.language, "cars.trip.delete_cancel"))

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
                pass
        self._exit_trip_select_mode()

    def _trip_detail_title(self, trip: Any) -> str:
        started = self._parse_ts(trip["started_at"])
        if started is None:
            return _translate(self.language, "cars.trip.title", id=int(trip["id"]))
        return started.strftime("%d.%m.%Y %H:%M")

    # ---------------------------------------------------- Scan-Liste & Detail

    def _render_scans_into_value_list(self) -> None:
        if self.db is None or self._selected_car_id is None:
            self.value_list.append(self._info_row(_translate(self.language, "cars.scans.empty")))
            return
        try:
            scans = self.db.list_scans_for_car(self._selected_car_id)
        except Exception:
            scans = []
        if not scans:
            self.value_list.append(self._info_row(_translate(self.language, "cars.scans.empty")))
            return
        for i, scan in enumerate(scans):
            prev = scans[i + 1] if i + 1 < len(scans) else None
            self.value_list.append(self._make_scan_row(scan, prev))

    def _make_scan_row(self, scan: Any, prev_scan: Any | None) -> Adw.ActionRow:
        row = Adw.ActionRow()
        ts = self._parse_ts(scan["scanned_at"])
        title = ts.strftime("%d.%m.%Y · %H:%M") if ts else str(scan["id"])
        row.set_title(GLib.markup_escape_text(title))

        dtc = int(scan["dtc_count"] or 0)
        pending = int(scan["pending_dtc_count"] or 0)
        pids = int(scan["pids_count"] or 0)

        # DTC trend vs. previous scan
        if prev_scan is None:
            trend = _translate(self.language, "cars.scan.trend_first")
        else:
            delta = dtc - int(prev_scan["dtc_count"] or 0)
            if delta > 0:
                trend = _translate(self.language, "cars.scan.trend_up", delta=delta)
            elif delta < 0:
                trend = _translate(self.language, "cars.scan.trend_down", delta=abs(delta))
            else:
                trend = _translate(self.language, "cars.scan.trend_same")

        parts = [
            f"{dtc} {_translate(self.language, 'cars.scan.dtc_count')}",
            f"{pending} {_translate(self.language, 'cars.scan.pending_count')}",
            f"{pids} {_translate(self.language, 'cars.scan.pids_count')}",
            trend,
        ]
        row.set_subtitle(GLib.markup_escape_text(" · ".join(parts)))
        row.set_activatable(True)

        # Colour-code the DTC count badge
        badge = Gtk.Label(label=str(dtc))
        badge.add_css_class("pill" if dtc == 0 else "error")
        badge.add_css_class("caption")
        badge.set_halign(Gtk.Align.END)
        row.add_suffix(badge)
        row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        row.connect("activated", lambda _r, sid=int(scan["id"]): self._open_scan_detail(sid))
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
            return
        if scan_meta is None:
            return

        page_content = _build_scan_detail_widget(self.language, scan_meta, prev_meta, data)
        ts = self._parse_ts(scan_meta["scanned_at"])
        title = _translate(self.language, "cars.scan.title",
                           date=ts.strftime("%d.%m.%Y %H:%M") if ts else str(scan_id))

        self._set_trash(lambda: self._confirm_delete_scan(scan_id))

        page = Adw.NavigationPage(child=page_content, title=title)
        page.set_tag(f"scan-{scan_id}")
        self._scan_detail_page = page
        self._scan_detail_pushed = True
        self._scan_id_shown = scan_id
        self.nav_view.push(page)

    # ------------------------------------------ Beschleunigungsläufe-Liste

    def _render_accel_runs_into_value_list(self) -> None:
        if self.db is None or self._selected_car_id is None:
            self.value_list.append(self._info_row(_translate(self.language, "cars.accel_runs.empty")))
            return
        try:
            runs = self.db.list_acceleration_runs_for_car(self._selected_car_id)
        except Exception:
            runs = []
        if not runs:
            self.value_list.append(self._info_row(_translate(self.language, "cars.accel_runs.empty")))
            return
        for run in runs:
            self.value_list.append(self._make_accel_run_row(run))

    def _make_accel_run_row(self, run: Any) -> Adw.ActionRow:
        row = Adw.ActionRow()
        run_id = int(run["id"])
        ts = self._parse_ts(run["run_at"])
        title = ts.strftime("%d.%m.%Y · %H:%M") if ts else _translate(self.language, "cars.accel_run.title", date=str(run_id))
        row.set_title(GLib.markup_escape_text(title))

        parts: list[str] = []
        try:
            data = self.db.get_acceleration_run(run_id)
            results = data.get("results", {})
            targets = results.get("targets", {})
            max_obd = results.get("max_obd_kmh")
            max_gps = results.get("max_gps_kmh")
            if max_obd is not None:
                parts.append(f"OBD {max_obd:.0f} km/h")
            if max_gps is not None:
                parts.append(f"GPS {max_gps:.0f} km/h")
            count = len([v for v in targets.values() if v.get("obd") is not None or v.get("gps") is not None])
            if count:
                parts.append(f"{count} Zeiten")
        except Exception:
            pass
        row.set_subtitle(GLib.markup_escape_text(" · ".join(parts)) if parts else "")

        icon = Gtk.Image.new_from_icon_name("stopwatch-symbolic")
        row.add_prefix(icon)
        chev = Gtk.Image.new_from_icon_name("go-next-symbolic")
        row.add_suffix(chev)
        row.set_activatable(True)
        row.connect("activated", lambda _r, rid=run_id: self._open_accel_run_detail(rid))
        return row

    def _open_accel_run_detail(self, run_id: int) -> None:
        if self.db is None:
            return
        try:
            data = self.db.get_acceleration_run(run_id)
        except Exception:
            return
        if not data:
            return

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_child(box)

        results = data.get("results", {})
        targets = results.get("targets", {})
        ranges = results.get("ranges", {})
        max_obd = results.get("max_obd_kmh")
        max_gps = results.get("max_gps_kmh")
        max_g = results.get("max_g")

        group = Adw.PreferencesGroup()
        group.set_margin_top(12)
        group.set_margin_bottom(12)
        group.set_margin_start(12)
        group.set_margin_end(12)
        box.append(group)

        def _add_row(title: str, val: str) -> None:
            r = Adw.ActionRow()
            r.set_title(GLib.markup_escape_text(title))
            lbl = Gtk.Label(label=val)
            lbl.add_css_class("monospace")
            lbl.set_valign(Gtk.Align.CENTER)
            r.add_suffix(lbl)
            r.set_activatable(False)
            group.add(r)

        if max_obd is not None:
            _add_row(_translate(self.language, "cars.accel_run.max_obd"), f"{max_obd:.0f} km/h")
        if max_gps is not None:
            _add_row(_translate(self.language, "cars.accel_run.max_gps"), f"{max_gps:.0f} km/h")
        if max_g is not None:
            _add_row(_translate(self.language, "cars.accel_run.max_g"), f"{max_g:.3f} g")

        for target_str in sorted(targets.keys(), key=lambda s: float(s)):
            v = targets[target_str]
            obd_t = v.get("obd")
            gps_t = v.get("gps")
            parts: list[str] = []
            if obd_t is not None:
                parts.append(f"OBD {obd_t:.2f} s")
            if gps_t is not None:
                parts.append(f"GPS {gps_t:.2f} s")
            if parts:
                _add_row(f"0–{target_str} km/h", "  ·  ".join(parts))

        for range_str, v in ranges.items():
            obd_t = v.get("obd")
            gps_t = v.get("gps")
            parts = []
            if obd_t is not None:
                parts.append(f"OBD {obd_t:.2f} s")
            if gps_t is not None:
                parts.append(f"GPS {gps_t:.2f} s")
            if parts:
                _add_row(f"{range_str} km/h", "  ·  ".join(parts))

        del_btn = Gtk.Button(label=_translate(self.language, "cars.accel_run.delete_title"))
        del_btn.add_css_class("destructive-action")
        del_btn.set_margin_top(8)
        del_btn.set_margin_bottom(16)
        del_btn.set_margin_start(12)
        del_btn.set_margin_end(12)
        del_btn.connect("clicked", lambda _b: self._confirm_delete_accel_run(run_id))
        box.append(del_btn)

        ts = self._parse_ts(data.get("run_at"))
        title = _translate(self.language, "cars.accel_run.title",
                           date=ts.strftime("%d.%m.%Y %H:%M") if ts else str(run_id))
        page = Adw.NavigationPage(child=scrolled, title=title)
        page.set_tag(f"accel-run-{run_id}")
        self.nav_view.push(page)

    def _confirm_delete_accel_run(self, run_id: int) -> None:
        dialog = Adw.AlertDialog(
            heading=_translate(self.language, "cars.accel_run.delete_title"),
            body=_translate(self.language, "cars.accel_run.delete_body"),
        )
        dialog.add_response("cancel", _translate(self.language, "cars.trip.delete_cancel"))
        dialog.add_response("delete", _translate(self.language, "cars.trip.delete_confirm"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda d, r: self._do_delete_accel_run(run_id) if r == "delete" else None)
        dialog.present(self)

    def _do_delete_accel_run(self, run_id: int) -> None:
        if self.db is None:
            return
        self.db.delete_acceleration_run(run_id)
        self.nav_view.pop()
        self._render_detail()

    def refresh_if_showing_car(self, car_id: int) -> None:
        if self._selected_car_id == car_id and self._detail_pushed and self._selected_category == "acceleration_runs":
            self._render_detail()

    def _make_stacked_row(self, label: str, value_text: str, is_unknown: bool) -> Gtk.ListBoxRow:
        """Titel oben, Wert rechtsbündig darunter — passend für lange Werte wie VIN."""
        row = Gtk.ListBoxRow()
        row.set_activatable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(14)
        box.set_margin_end(14)

        title_lbl = Gtk.Label(label=label, xalign=0.0)
        title_lbl.set_halign(Gtk.Align.START)
        title_lbl.set_hexpand(True)
        box.append(title_lbl)

        value_lbl = Gtk.Label(label=value_text, xalign=1.0)
        value_lbl.set_halign(Gtk.Align.END)
        value_lbl.set_hexpand(True)
        value_lbl.set_wrap(True)
        value_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        value_lbl.set_selectable(True)
        if is_unknown:
            value_lbl.add_css_class("dim-label")
        box.append(value_lbl)

        row.set_child(box)
        return row
