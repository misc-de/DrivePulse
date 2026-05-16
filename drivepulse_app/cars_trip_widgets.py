"""Trip detail widgets for the Cars page."""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .common import _translate
from .cars_metadata import _CHART_METRICS
from .cars_trip_visuals import _build_chart_widget, _build_osm_map_widget, _draw_gps_track


def _build_trip_detail_widget(language: str, trip: Any, samples: list[Any]) -> Gtk.Widget:
    """Stat-Karte + GPS-Track + Speed-Verlauf für eine einzelne Fahrt."""
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
    outer.set_margin_top(14)
    outer.set_margin_bottom(14)
    outer.set_margin_start(14)
    outer.set_margin_end(14)

    # --- Stats ---
    stats = Gtk.ListBox()
    stats.set_selection_mode(Gtk.SelectionMode.NONE)
    stats.add_css_class("boxed-list")
    stats.set_valign(Gtk.Align.START)

    def _add_stat(title: str, value: str) -> None:
        row = Adw.ActionRow()
        row.set_title(GLib.markup_escape_text(title))
        lbl = Gtk.Label(label=value, xalign=1.0)
        lbl.add_css_class("monospace")
        lbl.set_halign(Gtk.Align.END)
        row.add_suffix(lbl)
        stats.append(row)

    started = _safe_ts(trip["started_at"])
    ended = _safe_ts(trip["ended_at"])
    _add_stat(_translate(language, "cars.trip.start"), started.strftime("%d.%m.%Y %H:%M:%S") if started else "—")
    _add_stat(_translate(language, "cars.trip.end"), ended.strftime("%d.%m.%Y %H:%M:%S") if ended else "—")
    dur_s = trip["duration_s"] or 0.0
    if dur_s:
        hrs = int(dur_s // 3600)
        mins = int((dur_s % 3600) // 60)
        secs = int(dur_s % 60)
        dur_text = f"{hrs}:{mins:02d}:{secs:02d}" if hrs else f"{mins}:{secs:02d} min"
    else:
        dur_text = "—"
    _add_stat(_translate(language, "cars.trip.duration"), dur_text)
    _add_stat(_translate(language, "cars.trip.distance"), f"{trip['distance_km']:.2f} km" if trip["distance_km"] else "—")
    _add_stat(_translate(language, "cars.trip.max_speed"), f"{trip['max_speed_kmh']:.0f} km/h" if trip["max_speed_kmh"] else "—")
    _add_stat(_translate(language, "cars.trip.avg_speed"), f"{trip['avg_speed_kmh']:.0f} km/h" if trip["avg_speed_kmh"] else "—")
    _add_stat(_translate(language, "cars.trip.samples"), str(trip["samples_count"] or 0))

    outer.append(stats)

    # --- Build per-metric point lists: (ts, value|None, lat, lon) ---
    # Base: all samples that have GPS coordinates (needed for map cursor sync)
    _base = [s for s in samples if s["lat"] is not None and s["lon"] is not None]

    def _finite(v: Any) -> bool:
        """True only for finite, non-NaN numbers — rejects None, nan, inf, strings."""
        try:
            return math.isfinite(float(v))
        except (TypeError, ValueError):
            return False

    _min_valid = max(2, int(len(_base) * 0.30))  # mindestens 30 % der GPS-Samples
    metric_data: dict[str, list] = {}
    for _mk, _ml, _mu, _mc, _mf in _CHART_METRICS:
        _pts = [(s["ts"], s[_mk] if _finite(s[_mk]) else None, s["lat"], s["lon"])
                for s in _base]
        if sum(1 for p in _pts if p[1] is not None) >= _min_valid:
            metric_data[_mk] = _pts

    _avail = [(k, _translate(language, l), u, c, f) for k, l, u, c, f in _CHART_METRICS if k in metric_data]

    _def_key = "speed_kmh" if "speed_kmh" in metric_data else (
        _avail[0][0] if _avail else None
    )
    chart_state: dict[str, Any] = {}
    if _def_key:
        _dm = next(m for m in _CHART_METRICS if m[0] == _def_key)
        chart_state = {
            "pts": metric_data[_def_key],
            "unit": _dm[2],
            "color": _dm[3],
            "fmt": _dm[4],
            "key": _def_key,
        }

    # Shared cursor state: idx = index into chart_state["pts"], -1 = none
    cursor_state: dict[str, Any] = {"idx": -1}
    map_widget_ref: list[Any] = [None]
    map_center_ref: list[Any] = [None]
    chart_area_ref: list[Any] = [None]

    def _on_cursor_change() -> None:
        if map_center_ref[0] is not None and chart_state:
            idx = cursor_state.get("idx", -1)
            pts = chart_state.get("pts") or []
            if 0 <= idx < len(pts):
                clat, clon = pts[idx][2], pts[idx][3]
                if clat is not None and clon is not None:
                    map_center_ref[0](clat, clon)
        if map_widget_ref[0]:
            map_widget_ref[0].queue_draw()
        if chart_area_ref[0]:
            chart_area_ref[0].queue_draw()

    # --- GPS-Track / OSM Map ---
    gps_points = [(s["lat"], s["lon"], s["speed_kmh"]) for s in samples
                  if s["lat"] is not None and s["lon"] is not None]
    if gps_points:
        gps_title = Gtk.Label(label=_translate(language, "cars.trip.route"), xalign=0.0)
        gps_title.add_css_class("heading")
        outer.append(gps_title)
        map_result = _build_osm_map_widget(
            gps_points,
            chart_state=chart_state if chart_state else None,
            cursor_state=cursor_state,
        )
        if map_result is not None:
            map_widget, map_center_fn = map_result
            map_widget_ref[0] = map_widget
            map_center_ref[0] = map_center_fn
            outer.append(map_widget)
        else:
            gps_area = Gtk.DrawingArea()
            gps_area.set_content_height(240)
            gps_area.set_hexpand(True)
            gps_area.add_css_class("card")
            gps_area.set_draw_func(lambda area, cr, w, h, pts=gps_points: _draw_gps_track(cr, w, h, pts))
            outer.append(gps_area)

    # --- Datenverlauf ---
    if _avail and chart_state:
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header_box.set_hexpand(True)

        chart_title_lbl = Gtk.Label(xalign=0.0)
        chart_title_lbl.add_css_class("heading")
        chart_title_lbl.set_hexpand(True)
        _init_lbl = next((m[1] for m in _avail if m[0] == chart_state["key"]), _avail[0][1])
        chart_title_lbl.set_label(_init_lbl)
        header_box.append(chart_title_lbl)

        if len(_avail) > 1:
            _str_model = Gtk.StringList.new([m[1] for m in _avail])
            _dropdown = Gtk.DropDown.new(_str_model, None)
            _dropdown.set_valign(Gtk.Align.CENTER)
            _init_sel = next((i for i, m in enumerate(_avail) if m[0] == chart_state["key"]), 0)
            _dropdown.set_selected(_init_sel)

            def _on_metric_selected(dd: Gtk.DropDown, _pspec: Any, avail: list = _avail) -> None:
                sel = dd.get_selected()
                if 0 <= sel < len(avail):
                    key, lbl, unit, color, fmt = avail[sel]
                    chart_state["pts"] = metric_data[key]
                    chart_state["unit"] = unit
                    chart_state["color"] = color
                    chart_state["fmt"] = fmt
                    chart_state["key"] = key
                    chart_title_lbl.set_label(lbl)
                    cursor_state["idx"] = -1
                    if chart_area_ref[0]:
                        chart_area_ref[0].queue_draw()
                    if map_widget_ref[0]:
                        map_widget_ref[0].queue_draw()

            _dropdown.connect("notify::selected", _on_metric_selected)
            header_box.append(_dropdown)

        outer.append(header_box)
        sp_area = _build_chart_widget(chart_state, cursor_state, _on_cursor_change)
        chart_area_ref[0] = sp_area
        outer.append(sp_area)

    if not gps_points and not _avail:
        empty = Gtk.Label(label=_translate(language, "cars.trip.no_data"), xalign=0.0)
        empty.add_css_class("dim-label")
        outer.append(empty)

    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroll.set_vexpand(True)
    scroll.set_hexpand(True)
    scroll.set_child(outer)
    return scroll


def _safe_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None
