"""Autos-Browser: Liste bekannter Fahrzeuge → Detail mit kategorisierten Werten."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
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
    LIVE_KEY_TO_PID,
    _extract_inner_string,
    _wmi_to_brand,
)
from .cars_profiles import _load_profiles
from .diagnostics import get_logger
from .vin_api import fetch_vin_data

log = get_logger(__name__)
from .cars_stopwatch_runs import CarsStopWatchRunsMixin
from .cars_scans import CarsScansMixin
from .cars_photos import CarsPhotosMixin


def _extract_session_number(v: Any) -> "float | None":
    if isinstance(v, dict) and "value" in v:
        try:
            return float(v["value"])
        except (TypeError, ValueError):
            return None
    if isinstance(v, (int, float)):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------


class CarsPage(
    CarsActionsMixin,
    CarsLayoutMixin,
    CarsDetailRenderMixin,
    CarsTripsMixin,
    CarsScansMixin,
    CarsStopWatchRunsMixin,
    CarsPhotosMixin,
    Gtk.Box,
):
    """Zweistufige Navigation: Fahrzeug-Liste → Werte-Detail."""

    __gtype_name__ = "CarsPage"

    LIVE_ID = "__live__"
    LIVE_DETAIL_RENDER_INTERVAL_S = 0.25

    def __init__(
        self,
        language: str = SOURCE_LANGUAGE,
        db: DriveDB | None = None,
        sidebar_side: str = "left",
        vindecoder_api_key: str | None = None,
        vindecoder_secret_key: str | None = None,
        initial_source: str | None = None,
        initial_category: str | None = None,
        initial_scan_id: int | None = None,
        on_state_changed: Callable[[str | None, str | None, int | None], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.language = _normalize_language(language)
        self.db = db
        self._sidebar_side: str = sidebar_side
        self._vindecoder_api_key: str | None = vindecoder_api_key
        self._vindecoder_secret_key: str | None = vindecoder_secret_key
        self._initial_source: str | None = initial_source
        self._initial_category: str | None = initial_category
        self._initial_scan_id: int | None = initial_scan_id
        self.on_state_changed: Callable[[str | None, str | None, int | None], None] | None = on_state_changed
        # Suppress on_state_changed callbacks while restoring saved state or
        # during initial UI construction, so we don't write a "user opened
        # live/vehicle" entry just from default widget signals firing.
        # Flipped to False once __init__ has finished applying initial state.
        self._restoring_state: bool = True
        self._autodev_api_key: str | None = None
        self._vin_fetch_pending: set[int] = set()
        self._vin_review_queue: list[tuple[int, str, dict]] = []
        self._vin_review_open: bool = False
        self._latest_live: dict[str, Any] = {}
        self._live_identity: dict[str, str] = {}
        self._live_session_stats: dict[str, dict] = {}
        self.on_live_vehicle_add: Callable[[dict[str, str]], int | None] | None = None
        self._obd_connected = False
        self._profiles: list[dict[str, Any]] = []
        self._selected_source: str = self.LIVE_ID
        self._selected_car_id: int | None = None
        self._selected_category: str = CATEGORIES[0][0]
        self._detail_pushed = False
        self._trip_detail_pushed = False
        self._trip_detail_page: Adw.NavigationPage | None = None
        self._scan_detail_pushed = False
        self._scan_detail_page: Adw.NavigationPage | None = None
        self._scan_id_shown: int | None = None
        self._stopwatch_run_detail_page: Adw.NavigationPage | None = None
        self._live_row: Adw.ActionRow | None = None
        self._add_live_vehicle_btn: Gtk.Button | None = None
        self._last_live_detail_render = -self.LIVE_DETAIL_RENDER_INTERVAL_S
        self._narrow = False
        self._cat_rows: list[Gtk.ListBoxRow] = []
        self._trip_select_mode: bool = False
        self._trip_selected_ids: set[int] = set()
        self._scan_select_mode: bool = False
        self._scan_selected_ids: set[int] = set()
        self._selected_scan_id: int | None = None
        self._scan_pid_stats: dict[str, dict] = {}
        self._run_select_mode: bool = False
        self._run_selected_ids: set[int] = set()
        self._photo_select_mode: bool = False
        self._photo_selected_ids: set[int] = set()
        self._photo_detail_page: Adw.NavigationPage | None = None
        # Wird vom DashboardWindow gesetzt: Callback, wenn der Anwender auf der
        # Wurzel (Auto-Liste) nach rechts wischt, um zum vorherigen Tab zurückzukehren.
        self.on_back_swipe: Callable[[], None] | None = None
        self.on_forward_swipe: Callable[[], None] | None = None
        self.on_load_stopwatch_run: Callable[[dict], None] | None = None
        self.on_open_trip_as_route: Callable[
            [list[list[float]], float | None, float | None, str | None], None
        ] | None = None
        self._drag_claimed = False
        self.get_sync_client: Any = None
        # Mock mode disables share/rename so demo data isn't pushed to peers.
        self.mock_mode: bool = False

        # Content stack: holds the detail page as root, with sub-detail pages
        # (trip/scan/run/photo) pushed on top. The list lives in the sidebar
        # of the NavigationSplitView, not in this stack.
        self.nav_view = Adw.NavigationView()
        self.nav_view.set_hexpand(True)
        self.nav_view.set_vexpand(True)
        self.nav_view.connect("popped", self._on_popped)

        # NavigationSplitView: sidebar (list) + content (nav_view). When
        # ``collapsed`` is true the split view degrades to mobile-style
        # push/pop navigation between sidebar and content. The parent window
        # flips collapsed via :meth:`set_collapsed` based on form factor.
        self._split_view = Adw.NavigationSplitView()
        self._split_view.set_hexpand(True)
        self._split_view.set_vexpand(True)
        self._split_view.set_collapsed(True)
        self._split_view.set_show_content(False)
        self._split_view.set_min_sidebar_width(280)
        self._split_view.set_content(
            Adw.NavigationPage(
                child=self.nav_view,
                title=_translate(self.language, "nav.cars"),
            )
        )
        self._split_view.connect("notify::show-content", self._on_show_content_changed)
        self.append(self._split_view)

        self._build_list_page()
        self._build_detail_page()
        # Detail page is the permanent root of the content stack.
        self.nav_view.add(self._detail_page)
        self.refresh_profiles()
        # Restore last viewed source + category from persisted state. Falls
        # back silently if the saved car_id no longer exists in profiles.
        self._apply_initial_state()
        # End of construction — user interactions from here on persist state.
        self._restoring_state = False

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
        # Detail offen + Wisch nach rechts: wir poppen die Detail-Ansicht
        # selbst, damit der Anwender konsistent „nach rechts = zurück zur
        # Liste" bekommt, statt sich auf den Adw.NavigationView-Swipe zu
        # verlassen (der hier oft gar nicht greift).
        if self._detail_pushed and abs(offset_x) > 20 and offset_x > 0:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self._drag_claimed = True
            return
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
        # In der Detail-Ansicht: Rechts-Wisch = zurück zur Auto-Liste,
        # statt wie auf der Wurzel zum vorherigen Tab zu springen.
        if self._detail_pushed:
            if offset_x > 60:
                # Liegt eine Sub-Seite (Chart, Trip, Scan…) über der Detail-Wurzel,
                # poppt der Wisch nur eine Ebene → zurück zur Auto-Detailseite,
                # statt direkt bis in die Liste durchzubrechen.
                visible = self.nav_view.get_visible_page()
                if visible is not None and visible is not self._detail_page:
                    self.nav_view.pop()
                else:
                    self._on_detail_back()
            return
        if offset_x > 60 and self.on_back_swipe is not None:
            self.on_back_swipe()
        elif offset_x < -60 and self.on_forward_swipe is not None:
            self.on_forward_swipe()

    def set_language(self, language: str) -> None:
        self.language = _normalize_language(language)
        self._list_page.set_title(_translate(self.language, "nav.cars"))
        for sep in getattr(self, "_cat_section_rows", []):
            key = getattr(sep, "section_label_key", None)
            lbl = getattr(sep, "section_label_widget", None)
            if key and lbl:
                lbl.set_label(_translate(self.language, key))
        self._detail_back_btn.set_tooltip_text(_translate(self.language, "cars.back"))
        if self._add_live_vehicle_btn is not None:
            self._add_live_vehicle_btn.set_tooltip_text(_translate(self.language, "cars.live.add.tooltip"))
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
        self._update_live_add_button()
        self._schedule_vin_fetches()

    def _schedule_vin_fetches(self) -> None:
        if self.db is None:
            return
        for entry in self._profiles:
            car_id = entry.get("car_id")
            vin = entry.get("vin") or ""
            if not car_id or not vin or len(vin) < 11:
                continue
            if entry.get("vin_data_fetched"):
                continue
            if car_id in self._vin_fetch_pending:
                continue
            self._vin_fetch_pending.add(car_id)
            threading.Thread(
                target=self._fetch_vin_data_thread,
                args=(car_id, vin),
                daemon=True,
            ).start()

    def _fetch_vin_data_thread(self, car_id: int, vin: str) -> None:
        try:
            data = fetch_vin_data(
                vin,
                autodev_api_key=self._autodev_api_key,
                vindecoder_api_key=self._vindecoder_api_key,
                vindecoder_secret_key=self._vindecoder_secret_key,
            )
        except Exception:
            data = {}
        GLib.idle_add(self._on_vin_data_ready, car_id, vin, data)

    def _on_vin_data_ready(self, car_id: int, vin: str, sources: dict) -> bool:
        self._vin_fetch_pending.discard(car_id)
        if not sources:
            if self.db is not None:
                try:
                    self.db.update_car_vin_data(car_id, "{}")
                except Exception:
                    pass
            return False
        self._vin_review_queue.append((car_id, vin, sources))
        self._maybe_show_next_review()
        return False

    def _maybe_show_next_review(self) -> None:
        if self._vin_review_open or not self._vin_review_queue:
            return
        car_id, vin, data = self._vin_review_queue.pop(0)
        self._vin_review_open = True
        from .vin_review_dialog import VinReviewDialog
        dialog = VinReviewDialog(vin, data, self.language)

        def _on_response(d: "VinReviewDialog", response: str) -> None:
            self._vin_review_open = False
            if response == "accept":
                accepted = d.get_accepted_data()
            else:
                accepted = {}
            if self.db is not None:
                try:
                    self.db.update_car_vin_data(car_id, json.dumps(accepted))
                except Exception:
                    pass
            self._profiles = _load_profiles(self.db)
            self._rebuild_list()
            if self._detail_pushed:
                self._render_detail()
            self._maybe_show_next_review()

        dialog.connect("response", _on_response)
        root = self.get_root()
        if root:
            dialog.present(root)

    def update_live(self, payload: dict[str, Any]) -> None:
        if not payload:
            return
        source = payload.get("source", "")
        if source in ("obd", "mock"):
            for k, v in payload.items():
                if k.startswith("_") or k in ("source", "timestamp", "connection_status", "mock_reason"):
                    continue
                self._latest_live[k] = v
                if k in LIVE_KEY_TO_PID:
                    num = _extract_session_number(v)
                    if num is not None:
                        stats = self._live_session_stats.setdefault(k, {})
                        unit = v.get("unit", "") if isinstance(v, dict) else ""
                        stats["unit"] = unit
                        stats["min"] = num if "min" not in stats else min(stats["min"], num)
                        stats["max"] = num if "max" not in stats else max(stats["max"], num)
            self._obd_connected = source == "obd"
            self._update_live_row_subtitle()
        if self._selected_source == self.LIVE_ID and self._detail_pushed and self._live_detail_render_due():
            self._render_detail()

    def clear_live_session(self) -> None:
        """Verbindung getrennt — alle Live-Session-Daten zurücksetzen."""
        self._live_session_stats = {}
        self._latest_live = {}
        self._live_identity = {}
        self._obd_connected = False
        self._update_live_row_subtitle()
        self._update_live_add_button()
        if self._selected_source == self.LIVE_ID and self._detail_pushed:
            self._render_detail()

    def open_car(self, car_id: int) -> None:
        """Zur Detail-Ansicht eines bekannten Fahrzeugs navigieren."""
        self.refresh_profiles()
        self._open_detail(f"car:{car_id}")

    def set_live_identity(self, identity: dict[str, str]) -> None:
        self._live_identity = dict(identity)
        self._update_live_row_subtitle()
        self._update_live_add_button()
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
        # Live-Zeile bleibt oben fest verankert (eigene boxed-list über dem
        # Scroll-Bereich). Bei jedem Rebuild neu aufbauen, damit Übersetzungen
        # nach Sprachwechsel mitziehen.
        while True:
            child = self._live_list_box.get_first_child()
            if child is None:
                break
            self._live_list_box.remove(child)
        self._live_row = Adw.ActionRow()
        self._live_row.set_title(_translate(self.language, "cars.live.title"))
        self._live_row.set_activatable(True)
        live_icon = Gtk.Image.new_from_icon_name("network-wireless-symbolic")
        self._live_row.add_prefix(live_icon)
        chevron = Gtk.Image.new_from_icon_name("go-next-symbolic")
        self._live_row.add_suffix(chevron)
        self._live_row.connect("activated", lambda _r: self._open_detail(self.LIVE_ID))
        self._live_list_box.append(self._live_row)
        self._update_live_row_subtitle()

        # Scrollbare Auto-Liste neu befüllen
        while True:
            child = self._list_box.get_first_child()
            if child is None:
                break
            self._list_box.remove(child)

        for entry in self._profiles:
            row = Adw.ActionRow()
            vin = entry.get("vin", "")
            label = entry.get("label") or ""
            brand = entry.get("brand") or ""
            title = label or brand or (f"VIN …{vin[-4:]}" if vin else _translate(self.language, "cars.unknown"))
            row.set_title(GLib.markup_escape_text(title))
            sub_parts: list[str] = []
            if vin:
                sub_parts.append(f"VIN …{vin[-4:]}")
            latest_scan_at = entry.get("latest_scan_at")
            if latest_scan_at:
                dtc_count = int(entry.get("latest_dtc_count") or 0)
                sub_parts.append(
                    f"{_translate(self.language, 'cars.list.errors')}: {dtc_count}"
                )
                try:
                    scan_dt = datetime.fromisoformat(str(latest_scan_at).replace("Z", "+00:00"))
                    sub_parts.append(
                        f"{_translate(self.language, 'cars.list.scan')}: {scan_dt.strftime('%d.%m.%Y')}"
                    )
                except Exception:
                    pass
            row.set_subtitle(GLib.markup_escape_text(" · ".join(sub_parts)) if sub_parts else "—")
            row.set_activatable(True)
            chev = Gtk.Image.new_from_icon_name("go-next-symbolic")
            row.add_suffix(chev)
            row.connect("activated", lambda _r, p=str(entry["path"]): self._open_detail(p))
            self._list_box.append(row)

        self._list_box.set_visible(bool(self._profiles))
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

    def _live_vin(self) -> str:
        return _extract_inner_string(self._live_identity.get("VIN"))

    def _live_vehicle_is_known(self) -> bool:
        vin = self._live_vin()
        if not vin:
            return False
        return any(
            entry.get("car_id") is not None and entry.get("vin") == vin
            for entry in self._profiles
        )

    def _update_live_add_button(self) -> None:
        btn = self._add_live_vehicle_btn
        if btn is None:
            return
        show = (
            self._selected_source == self.LIVE_ID
            and bool(self._live_vin())
            and not self._live_vehicle_is_known()
        )
        btn.set_visible(show)

    # ---------------------------------------------------- Detail-Navigation

    _LIVE_HIDDEN_CATS = frozenset({"trips", "stopwatch_runs", "scans", "photos"})

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

    def _select_scan(self, scan_id: int) -> None:
        # Set the scan as the active context for data lookups (Stammdaten,
        # Diagnose, etc. follow this scan) but stay on the Scan-Verläufe
        # category visually — switching to vehicle/Stammdaten yanked the
        # user out of the list whenever they tapped an entry.
        self._selected_scan_id = scan_id
        self._persist_state()
        if not self._detail_pushed:
            return
        # Re-render so the checkmark on the now-selected scan appears.
        self._render_detail()

    def _bg_compute_scan_stats(self) -> None:
        stats: dict[str, dict] = {}
        if self.db is None or self._selected_car_id is None:
            GLib.idle_add(self._apply_scan_pid_stats, stats)
            return
        try:
            scans = self.db.list_scans_for_car(self._selected_car_id)
        except Exception:
            GLib.idle_add(self._apply_scan_pid_stats, stats)
            return
        from .cars_metadata import _parse_profile_pid_key
        raw_values: dict[str, list[tuple[str, float]]] = {}
        for scan_meta in scans:
            ts_str = str(scan_meta["scanned_at"] or "")
            try:
                data = self.db.get_scan_data(int(scan_meta["id"]))
            except Exception:
                continue
            for raw_key, raw_val in (data.get("live_data") or {}).items():
                pid = _parse_profile_pid_key(raw_key)
                if not pid:
                    continue
                if isinstance(raw_val, dict):
                    v = raw_val.get("value")
                    unit = str(raw_val.get("unit", ""))
                else:
                    v = raw_val
                    unit = ""
                try:
                    num = float(v)
                except (TypeError, ValueError):
                    continue
                if pid not in stats:
                    stats[pid] = {"min": num, "max": num, "sum": num, "count": 1, "unit": unit}
                else:
                    stats[pid]["min"] = min(stats[pid]["min"], num)
                    stats[pid]["max"] = max(stats[pid]["max"], num)
                    stats[pid]["sum"] += num
                    stats[pid]["count"] += 1
                raw_values.setdefault(pid, []).append((ts_str, num))
        for pid, s in stats.items():
            s["avg"] = s["sum"] / s["count"]
            pts = raw_values.get(pid) or []
            pts.sort(key=lambda t: t[0])
            s["values"] = pts
        GLib.idle_add(self._apply_scan_pid_stats, stats)

    def _apply_scan_pid_stats(self, stats: dict) -> bool:
        self._scan_pid_stats = stats
        if self._detail_pushed and self._selected_source != self.LIVE_ID:
            self._render_detail()
        return False

    def _open_detail(self, source: str) -> None:
        self._selected_source = source
        self._selected_scan_id = None
        self._scan_pid_stats = {}
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
        self._rename_btn.set_visible(is_real_car and not self.mock_mode)
        has_vin = bool(entry.get("vin")) if (is_real_car and entry) else False
        self._vin_refresh_btn.set_visible(is_real_car and has_vin and not self.mock_mode)
        self._update_live_add_button()
        self._update_category_visibility(source == self.LIVE_ID)
        self._render_detail()
        self._update_photo_upload_btn_visibility()
        if self._selected_car_id is not None:
            threading.Thread(target=self._bg_compute_scan_stats, daemon=True).start()
        self._detail_pushed = True
        # In collapsed (mobile) layout this slides the detail in over the list.
        # In uncollapsed (desktop) layout both panes are already visible; this
        # only updates the internal show-content flag for later use.
        self._split_view.set_show_content(True)
        self._persist_state()

    def _on_popped(self, _view: Adw.NavigationView, page: Adw.NavigationPage) -> None:
        # The detail page is the permanent root of the content stack and is
        # never popped — leaving the detail view is handled by
        # ``_on_show_content_changed`` via the NavigationSplitView.
        if page is self._detail_page:
            self._reset_detail_state()
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
        if page is self._stopwatch_run_detail_page:
            self._stopwatch_run_detail_page = None
            if self._detail_pushed and self._selected_category == "stopwatch_runs":
                self._render_detail()
            if self._detail_pushed and self._selected_car_id is not None:
                self._set_trash(self._confirm_delete_vehicle)
        if page is self._photo_detail_page:
            self._photo_detail_page = None
            if self._detail_pushed and self._selected_category == "photos":
                self._render_detail()
            if self._detail_pushed and self._selected_car_id is not None:
                self._set_trash(self._confirm_delete_vehicle)
            self._update_photo_upload_btn_visibility()

    def _apply_initial_state(self) -> None:
        """Restore the source + category the user was last viewing."""
        src = self._initial_source
        if not src:
            return
        # LIVE is ephemeral (depends on what's currently connected) — never
        # auto-restore it. Restarting should land on the Cars overview, not
        # on a live-connection detail page.
        if src == self.LIVE_ID:
            return
        valid = any(str(e.get("path")) == src for e in self._profiles)
        if not valid:
            return
        self._restoring_state = True
        try:
            self._open_detail(src)
            # Restore scan_id before select_row so the _render_detail triggered
            # synchronously by _on_category_selected already shows the green icon.
            # (_open_detail resets _selected_scan_id to None, so we set it here.)
            if self._initial_scan_id is not None:
                self._selected_scan_id = self._initial_scan_id
            elif self._initial_category == "scans":
                # No specific scan saved yet — default to the most recent one so
                # the green marker and sidebar date reflect a concrete entry.
                try:
                    if self.db is not None and self._selected_car_id is not None:
                        scans = self.db.list_scans_for_car(self._selected_car_id)
                        if scans:
                            self._selected_scan_id = int(scans[0]["id"])
                except Exception:
                    pass
            cat = self._initial_category
            if cat:
                for row in self._cat_rows:
                    if getattr(row, "cat_key", "") == cat:
                        self.category_list.select_row(row)
                        break
        finally:
            self._restoring_state = False
        # Re-render once the widget hierarchy is realised. _render_detail at
        # this point runs while the cars_page isn't yet appended to the
        # dashboard window, and some Gtk widgets (notably the scan-date
        # listbox row) don't honour set_visible(True) until they've seen at
        # least one allocation pass. Re-firing the render on idle catches
        # this so the date appears on the first cars-tab open after restart.
        GLib.idle_add(self._render_detail_if_pushed)

    def _render_detail_if_pushed(self) -> bool:
        if self._detail_pushed:
            self._render_detail()
        return False  # one-shot idle

    def _persist_state(self) -> None:
        if self._restoring_state:
            return
        cb = self.on_state_changed
        if cb is None:
            return
        try:
            cb(self._selected_source, self._selected_category, self._selected_scan_id)
        except Exception:
            pass

    def _reset_detail_state(self) -> None:
        """Drop transient detail-view state when leaving / re-entering detail."""
        self._detail_pushed = False
        self._trip_select_mode = False
        self._trip_selected_ids = set()
        self._scan_select_mode = False
        self._scan_selected_ids = set()
        self._selected_scan_id = None
        self._scan_pid_stats = {}
        self._run_select_mode = False
        self._run_selected_ids = set()
        self._photo_select_mode = False
        self._photo_selected_ids = set()
        self._photo_detail_page = None
        self._set_trash(None)
        self._rename_btn.set_visible(False)
        self._vin_refresh_btn.set_visible(False)
        self._update_photo_upload_btn_visibility()
        # User left the detail view — clear persisted state so the next
        # startup shows the list, not the previously open detail page.
        if not self._restoring_state and self.on_state_changed is not None:
            try:
                self.on_state_changed(None, None)
            except Exception:
                pass

    def _on_detail_back(self) -> None:
        """Detail back-button: collapse-aware navigation back to the list."""
        self._split_view.set_show_content(False)
        # In uncollapsed mode show-content stays True visually; explicitly
        # reset state so a subsequent _open_detail starts fresh.
        if not self._split_view.get_collapsed():
            self._reset_detail_state()

    def _on_show_content_changed(self, *_args: Any) -> None:
        """User navigated back to the sidebar (collapsed layout only)."""
        if not self._split_view.get_collapsed():
            return
        if self._split_view.get_show_content():
            return
        # Pop any sub-detail pages back to the detail root so re-entering
        # detail starts on the vehicle overview, matching pre-split behavior.
        try:
            self.nav_view.pop_to_page(self._detail_page)
        except Exception:
            while self.nav_view.pop():
                pass
        self._reset_detail_state()

    def set_collapsed(self, collapsed: bool) -> None:
        """Toggle the NavigationSplitView between mobile and desktop modes."""
        # Mobile-only spacer rows in the categories list are toggled on every
        # call (not just transitions) so they end up matching the target
        # collapsed state even when the value doesn't actually change.
        for spacer in getattr(self, "_cat_mobile_spacer_rows", []):
            spacer.set_visible(collapsed)
        if self._split_view.get_collapsed() == collapsed:
            return
        self._split_view.set_collapsed(collapsed)
        # Desktop: both panes visible — hide the detail back button (the list
        # is right there). Mobile: keep it for the slide-back gesture.
        if hasattr(self, "_detail_back_btn"):
            self._detail_back_btn.set_visible(collapsed)
        if not collapsed:
            # Desktop view shows sidebar + detail side-by-side. Don't auto-open
            # the live view here — the user landing on "Cars" expects the
            # overview, not a forced detail page that then sticks across
            # restarts via the persisted last-source.
            if self._detail_pushed:
                self._split_view.set_show_content(True)
                # Re-render so form-factor-aware widgets (scan-date stack,
                # etc.) swap their layout to the desktop variant.
                self._render_detail()
        elif self._detail_pushed:
            # Collapsed → mobile layout. Re-render so the scan-date label
            # switches to the three-line stack.
            self._render_detail()

    def _is_sync_active(self) -> bool:
        if self.mock_mode:
            return False
        return callable(self.get_sync_client) and self.get_sync_client() is not None

    def set_mock_mode(self, mock_mode: bool) -> None:
        """Toggle mock-data mode. Hides share/rename affordances when on."""
        new_val = bool(mock_mode)
        if new_val == self.mock_mode:
            return
        self.mock_mode = new_val
        # Reflect immediately in the detail header buttons; the next list
        # re-render will drop the per-row share/rename callbacks via the
        # _is_sync_active() and mock_mode checks.
        if hasattr(self, "_detail_share_btn"):
            self._detail_share_btn.set_visible(self._is_sync_active() and self._detail_pushed)
        if hasattr(self, "_rename_btn"):
            self._rename_btn.set_visible(not self.mock_mode and self._detail_pushed)
        self.refresh()

    def refresh(self) -> None:
        """Reload profiles and the current detail; safe no-op if not built."""
        try:
            if self.db is not None:
                self._profiles = _load_profiles(self.db)
            if hasattr(self, "_render_detail") and self._detail_pushed:
                self._render_detail()
        except Exception:
            log.exception("Could not refresh cars page")

    def _set_trash(self, action_fn: Any) -> None:
        btn = self._detail_trash_btn
        if self._detail_trash_handler is not None:
            btn.disconnect(self._detail_trash_handler)
            self._detail_trash_handler = None
        # Mock mode: never expose a destructive action — the seeded vehicles
        # and their records must stay intact.
        if self.mock_mode:
            action_fn = None
        if action_fn is not None:
            self._detail_trash_handler = btn.connect("clicked", lambda _b: action_fn())
            btn.set_visible(True)
            self._detail_share_btn.set_visible(self._is_sync_active())
        else:
            btn.set_visible(False)
            self._detail_share_btn.set_visible(False)

    def _on_share_btn_clicked(self) -> None:
        if self._trip_select_mode:
            self._share_selected_trips()
        elif self._scan_select_mode:
            self._share_selected_scans()
        elif self._run_select_mode:
            self._share_selected_runs()
        elif self._photo_select_mode:
            self._share_selected_photos()
        else:
            self._share_vehicle()

    def _share_vehicle(self) -> None:
        from .share_flow import ShareFlow
        ShareFlow(self, self.db, self.language, self.get_sync_client).share_vehicle(
            self._selected_source, self._selected_car_id
        )

    def _share_trip(self, trip_id: int) -> None:
        dialog = Adw.AlertDialog(
            heading=_translate(self.language, "share.trip_confirm_title"),
            body=_translate(self.language, "share.trip_confirm_body"),
        )
        dialog.add_response("cancel", _translate(self.language, "share.cancel"))
        dialog.add_response("send", _translate(self.language, "share.send"))
        dialog.set_response_appearance("send", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("send")
        dialog.set_close_response("cancel")

        def _on_response(_d: Adw.AlertDialog, resp: str) -> None:
            if resp != "send":
                return
            from .share_flow import ShareFlow
            ShareFlow(self, self.db, self.language, self.get_sync_client).share_trips(
                self._selected_car_id, [trip_id]
            )

        dialog.connect("response", _on_response)
        dialog.present(self)

    def _share_selected_trips(self) -> None:
        ids = list(self._trip_selected_ids)
        self._exit_trip_select_mode()
        from .share_flow import ShareFlow
        ShareFlow(self, self.db, self.language, self.get_sync_client).share_trips(
            self._selected_car_id, ids
        )

    def _share_selected_scans(self) -> None:
        ids = list(self._scan_selected_ids)
        self._exit_scan_select_mode()
        from .share_flow import ShareFlow
        ShareFlow(self, self.db, self.language, self.get_sync_client).share_scans(
            self._selected_car_id, ids
        )

    def _share_selected_runs(self) -> None:
        ids = list(self._run_selected_ids)
        self._exit_run_select_mode()
        from .share_flow import ShareFlow
        ShareFlow(self, self.db, self.language, self.get_sync_client).share_runs(
            self._selected_car_id, ids
        )

    def _share_selected_photos(self) -> None:
        ids = list(self._photo_selected_ids)
        self._exit_photo_select_mode()
        from .share_flow import ShareFlow
        ShareFlow(self, self.db, self.language, self.get_sync_client).share_photos(
            self._selected_car_id, ids
        )

    def _share_run(self, run_id: int) -> None:
        from .share_flow import ShareFlow
        ShareFlow(self, self.db, self.language, self.get_sync_client).share_run(
            self._selected_car_id, run_id
        )

    def _share_scan(self, scan_id: int) -> None:
        from .share_flow import ShareFlow
        ShareFlow(self, self.db, self.language, self.get_sync_client).share_scan(
            self._selected_car_id, scan_id
        )

    def _on_category_selected(self, _box: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return
        # Section divider rows (e.g. "OBD Daten") have no cat_key — ignore.
        if not hasattr(row, "cat_key"):
            return
        new_cat = getattr(row, "cat_key", CATEGORIES[0][0])
        if self._trip_select_mode and new_cat != "trips":
            self._trip_select_mode = False
            self._trip_selected_ids = set()
            if self._selected_car_id is not None:
                self._set_trash(self._confirm_delete_vehicle)
            else:
                self._set_trash(None)
        if self._scan_select_mode and new_cat != "scans":
            self._scan_select_mode = False
            self._scan_selected_ids = set()
            if self._selected_car_id is not None:
                self._set_trash(self._confirm_delete_vehicle)
            else:
                self._set_trash(None)
        if self._run_select_mode and new_cat != "stopwatch_runs":
            self._run_select_mode = False
            self._run_selected_ids = set()
            if self._selected_car_id is not None:
                self._set_trash(self._confirm_delete_vehicle)
            else:
                self._set_trash(None)
        if self._photo_select_mode and new_cat != "photos":
            self._photo_select_mode = False
            self._photo_selected_ids = set()
            if self._selected_car_id is not None:
                self._set_trash(self._confirm_delete_vehicle)
            else:
                self._set_trash(None)
        self._selected_category = new_cat
        # Entering the scans list with no scan picked yet → highlight the most
        # recent one so the green marker reflects a concrete entry.
        if (
            new_cat == "scans"
            and self._selected_scan_id is None
            and self._selected_car_id is not None
            and self.db is not None
        ):
            try:
                scans = self.db.list_scans_for_car(self._selected_car_id)
                if scans:
                    self._selected_scan_id = int(scans[0]["id"])
            except Exception:
                pass
        self._update_photo_upload_btn_visibility()
        if self._detail_pushed:
            self._render_detail()
        self._persist_state()
