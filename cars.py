"""Autos-Browser: Liste bekannter Fahrzeuge → Detail mit kategorisierten Werten."""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from common import PROFILES_DIR, SOURCE_LANGUAGE, _normalize_language, _translate


# ---------------------------------------------------------------------------
# PID-Metadaten: Kategorien + endkundenfreundliche Bezeichnungen
# ---------------------------------------------------------------------------

LIVE_KEY_TO_PID: dict[str, str] = {
    "rpm":                    "010C",
    "speed":                  "010D",
    "coolant_temp":           "0105",
    "throttle_pos":           "0111",
    "engine_load":            "0104",
    "intake_temp":            "010F",
    "maf":                    "0110",
    "fuel_level":             "012F",
    "runtime":                "011F",
    "control_module_voltage": "0142",
}

_SPECIAL_VIN = "__VIN__"
_SPECIAL_CAL = "__CAL__"
_SPECIAL_CVN = "__CVN__"
_SPECIAL_PROTO = "__PROTO__"
_SPECIAL_SCAN_DATE = "__SCAN_DATE__"
_SPECIAL_DTC = "__DTC__"
_SPECIAL_PENDING = "__PENDING_DTC__"
_SPECIAL_ADAPTER_V = "__ATRV__"

CATEGORIES: tuple[tuple[str, str, str, tuple[tuple[str, str], ...]], ...] = (
    ("vehicle", "Fahrzeug", "dialog-information-symbolic", (
        (_SPECIAL_VIN,        "Fahrgestellnummer (VIN)"),
        (_SPECIAL_CAL,        "Steuergerät-Software (Cal-ID)"),
        (_SPECIAL_CVN,        "Software-Prüfnummer (CVN)"),
        (_SPECIAL_PROTO,      "Diagnose-Protokoll"),
        ("011C",              "OBD-Norm"),
        (_SPECIAL_SCAN_DATE,  "Letzter Scan"),
    )),
    ("engine", "Motor", "applications-engineering-symbolic", (
        ("010C", "Drehzahl"),
        ("0104", "Motorlast (berechnet)"),
        ("0143", "Motorlast (absolut)"),
        ("010E", "Zündzeitpunkt"),
        ("011F", "Motorlaufzeit seit Start"),
        ("0142", "Bordnetzspannung"),
        (_SPECIAL_ADAPTER_V, "Adapter-Spannung"),
    )),
    ("drive", "Geschwindigkeit & Strecke", "media-seek-forward-symbolic", (
        ("010D", "Geschwindigkeit"),
        ("0131", "Strecke seit Fehlerlöschung"),
        ("0121", "Strecke mit Motorkontrollleuchte"),
        ("0130", "Warmlaufzyklen seit Fehlerlöschung"),
    )),
    ("temperatures", "Temperaturen", "weather-clear-symbolic", (
        ("0105", "Kühlmittel"),
        ("010F", "Ansaugluft"),
        ("0146", "Außenluft"),
        ("013C", "Katalysator (Bank 1, Sensor 1)"),
    )),
    ("throttle", "Gas & Drosselklappe", "emblem-system-symbolic", (
        ("0111", "Drosselklappe"),
        ("0145", "Drosselklappe (relativ)"),
        ("0147", "Drosselklappe Sensor B"),
        ("0149", "Gaspedal Sensor D"),
        ("014A", "Gaspedal Sensor E"),
        ("014C", "Drosselklappen-Sollwert"),
    )),
    ("mixture", "Gemisch & Lambda", "applications-science-symbolic", (
        ("0103", "Kraftstoffsystem-Status"),
        ("0106", "Kurzzeit-Korrektur (Bank 1)"),
        ("0107", "Langzeit-Korrektur (Bank 1)"),
        ("0156", "Langzeit-Korrektur Sekundärsonde (Bank 1)"),
        ("0134", "Lambda Bank 1, Sensor 1"),
        ("0144", "Lambda-Sollwert"),
        ("0115", "Lambdasonde Bank 1, Sensor 2"),
    )),
    ("fuel", "Kraftstoff & Luft", "weather-windy-symbolic", (
        ("0110", "Luftmasse (MAF)"),
        ("012F", "Tankfüllstand"),
        ("0123", "Kraftstoff-Raildruck"),
        ("012E", "Tankentlüftung"),
        ("0133", "Luftdruck"),
    )),
    ("diagnostics", "Diagnose", "dialog-warning-symbolic", (
        (_SPECIAL_DTC,        "Gespeicherte Fehler"),
        (_SPECIAL_PENDING,    "Ausstehende Fehler"),
        ("0141", "Monitor-Status diese Fahrt"),
    )),
)


_UNIT_DISPLAY: dict[str, str] = {
    "revolutions_per_minute": "U/min",
    "kilometer_per_hour":     "km/h",
    "kilometer":              "km",
    "degree_Celsius":         "°C",
    "degree":                 "°",
    "percent":                "%",
    "kilopascal":             "kPa",
    "volt":                   "V",
    "second":                 "s",
    "milliampere":            "mA",
    "gps":                    "g/s",
    "ratio":                  "",
    "count":                  "",
    "rpm":                    "U/min",
    "km/h":                   "km/h",
    "degC":                   "°C",
    "deg":                    "°",
    "g":                      "g",
}

_WMI_BRANDS: dict[str, str] = {
    "WAU": "Audi", "TRU": "Audi", "WUA": "Audi",
    "WBA": "BMW", "WBS": "BMW", "WBY": "BMW",
    "WVW": "VW", "WV1": "VW", "WV2": "VW", "WVG": "VW",
    "WP0": "Porsche", "WP1": "Porsche",
    "WDB": "Mercedes-Benz", "WDD": "Mercedes-Benz", "WDC": "Mercedes-Benz",
    "VF1": "Renault", "VF3": "Peugeot", "VF7": "Citroën",
    "ZFA": "Fiat", "ZAR": "Alfa Romeo",
    "JT1": "Toyota", "JT2": "Toyota", "JTD": "Toyota",
    "JN1": "Nissan", "JN8": "Nissan",
    "KMH": "Hyundai", "KNA": "Kia",
    "VS5": "SEAT", "VSS": "SEAT", "TMB": "Škoda",
    "WF0": "Ford", "1FT": "Ford", "1FA": "Ford",
    "YV1": "Volvo", "YV4": "Volvo",
}


# ---------------------------------------------------------------------------
# Helfer
# ---------------------------------------------------------------------------


def _extract_inner_string(raw: Any) -> str:
    if raw is None:
        return ""
    s = str(raw)
    m = re.search(r"b['\"]([^'\"]+)['\"]", s)
    if m:
        return m.group(1)
    return s.strip()


def _wmi_to_brand(vin: str) -> str:
    return _WMI_BRANDS.get(vin[:3], "")


def _parse_profile_pid_key(key: str) -> str:
    m = re.search(r"b['\"]([0-9A-Fa-f]+)['\"]", key)
    return m.group(1).upper() if m else ""


def _format_status_string(value: str) -> str:
    if value.startswith("(") and "'" in value:
        m = re.match(r"^\(\s*'([^']*)'", value)
        if m:
            return m.group(1)
    return value


def _format_value_unit(payload: Any) -> str:
    if payload is None:
        return "—"
    if isinstance(payload, dict) and "value" in payload:
        value = payload.get("value")
        unit = payload.get("unit") or ""
        unit_disp = _UNIT_DISPLAY.get(unit, unit)
        if value is None:
            return "—"
        try:
            v = float(value)
        except (TypeError, ValueError):
            return f"{value} {unit_disp}".strip()
        if abs(v) >= 100:
            text = f"{v:.0f}"
        elif abs(v) >= 10:
            text = f"{v:.1f}"
        else:
            text = f"{v:.2f}"
        return f"{text} {unit_disp}".strip() if unit_disp else text
    if isinstance(payload, str):
        return _format_status_string(payload) or "—"
    return str(payload)


def _format_scan_date(raw: Any) -> str:
    if not raw:
        return "—"
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(raw)


def _load_profiles() -> list[dict[str, Any]]:
    if not PROFILES_DIR.exists():
        return []
    entries: list[dict[str, Any]] = []
    for path in sorted(PROFILES_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        vin = _extract_inner_string(data.get("vin"))
        brand = _wmi_to_brand(vin)
        try:
            dt = datetime.fromisoformat(str(data.get("scanned_at", "")).replace("Z", "+00:00"))
            scan_label = dt.strftime("%d.%m.%Y")
        except Exception:
            scan_label = ""
        entries.append({
            "path": path,
            "data": data,
            "vin": vin,
            "brand": brand,
            "scan_label": scan_label,
        })
    return entries


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------


class CarsPage(Gtk.Box):
    """Zweistufige Navigation: Fahrzeug-Liste → Werte-Detail."""

    __gtype_name__ = "CarsPage"

    LIVE_ID = "__live__"

    def __init__(self, language: str = SOURCE_LANGUAGE) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.language = _normalize_language(language)
        self._latest_live: dict[str, Any] = {}
        self._live_identity: dict[str, str] = {}
        self._obd_connected = False
        self._profiles: list[dict[str, Any]] = []
        self._selected_source: str = self.LIVE_ID
        self._selected_category: str = CATEGORIES[0][0]
        self._detail_pushed = False
        self._live_row: Adw.ActionRow | None = None
        self._narrow = False
        self._cat_rows: list[Gtk.ListBoxRow] = []

        self.nav_view = Adw.NavigationView()
        self.nav_view.set_hexpand(True)
        self.nav_view.set_vexpand(True)
        self.nav_view.connect("popped", self._on_popped)
        self.append(self.nav_view)

        self._build_list_page()
        self._build_detail_page()
        self.refresh_profiles()

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
        head.append(self._detail_back_btn)
        head.append(self._detail_title)
        outer.append(head)
        outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        body.set_hexpand(True)
        body.set_vexpand(True)

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
        for cat_key, cat_name, icon_name, _items in CATEGORIES:
            row = Gtk.ListBoxRow()
            row.cat_key = cat_key  # type: ignore[attr-defined]
            row.cat_name = cat_name  # type: ignore[attr-defined]

            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            hbox.set_margin_top(8)
            hbox.set_margin_bottom(8)
            hbox.set_margin_start(8)
            hbox.set_margin_end(8)

            icon = Gtk.Image.new_from_icon_name(icon_name)
            icon.set_pixel_size(18)
            hbox.append(icon)

            lbl = Gtk.Label(label=cat_name, xalign=0.0)
            lbl.set_hexpand(True)
            hbox.append(lbl)

            row.cat_label_widget = lbl  # type: ignore[attr-defined]
            row.cat_icon_widget = icon  # type: ignore[attr-defined]
            row.cat_hbox = hbox  # type: ignore[attr-defined]
            row.set_tooltip_text(cat_name)

            row.set_child(hbox)
            self.category_list.append(row)
            self._cat_rows.append(row)

        cat_scroll = Gtk.ScrolledWindow()
        cat_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        cat_scroll.set_vexpand(True)
        cat_scroll.set_child(self.category_list)
        self._sidebar.append(cat_scroll)
        body.append(self._sidebar)
        body.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        self._apply_narrow_to_sidebar()

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        content.set_hexpand(True)
        content.set_vexpand(True)

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

        body.append(content)
        outer.append(body)

        self._categories_label.set_text(_translate(self.language, "cars.categories"))

        self._detail_page = Adw.NavigationPage(child=outer, title="")
        self._detail_page.set_tag("detail")

        first_row = self.category_list.get_row_at_index(0)
        if first_row is not None:
            self.category_list.select_row(first_row)

    # ---------------------------------------------------- öffentliche API

    def set_narrow(self, narrow: bool) -> None:
        """Auf Smartphone-Breiten: Labels ausblenden, nur Icons zeigen."""
        if narrow == self._narrow:
            return
        self._narrow = narrow
        self._apply_narrow_to_sidebar()

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
        if self._detail_pushed:
            self._render_detail()

    def refresh_profiles(self) -> None:
        self._profiles = _load_profiles()
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
        if self._selected_source == self.LIVE_ID and self._detail_pushed:
            self._render_detail()

    def set_live_identity(self, identity: dict[str, str]) -> None:
        self._live_identity = dict(identity)
        self._update_live_row_subtitle()
        if self._selected_source == self.LIVE_ID and self._detail_pushed:
            self._render_detail()

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
            brand = entry.get("brand") or ""
            title = brand if brand else (f"VIN …{vin[-5:]}" if vin else _translate(self.language, "cars.unknown"))
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
        if source == self.LIVE_ID:
            title = _translate(self.language, "cars.live.title")
        else:
            entry = next((e for e in self._profiles if str(e["path"]) == source), None)
            if entry:
                vin = entry.get("vin", "")
                brand = entry.get("brand") or ""
                title = brand if brand else _translate(self.language, "cars.unknown")
                if vin:
                    title = f"{title} · …{vin[-5:]}"
            else:
                title = _translate(self.language, "cars.unknown")
        self._detail_page.set_title(title)
        self._detail_title.set_text(title)
        self._render_detail()
        if not self._detail_pushed:
            self.nav_view.push(self._detail_page)
            self._detail_pushed = True

    def _on_popped(self, _view: Adw.NavigationView, page: Adw.NavigationPage) -> None:
        if page is self._detail_page:
            self._detail_pushed = False

    def _on_category_selected(self, _box: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return
        self._selected_category = getattr(row, "cat_key", CATEGORIES[0][0])
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
        _cat_key, cat_name, _icon_name, items = cat_meta
        self.content_title.set_text(cat_name)

        data, source_label = self._current_data()
        self.content_subtitle.set_text(
            _translate(self.language, "cars.source.label", source=source_label)
        )

        for pid_key, label in items:
            raw = data.get(pid_key)
            value_text, is_unknown = self._format_entry(pid_key, raw)

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

            self.value_list.append(row)
