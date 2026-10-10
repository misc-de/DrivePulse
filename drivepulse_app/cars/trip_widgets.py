"""Trip detail widgets for the Cars page."""
from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from drivepulse_app.cars.metadata import _CHART_METRICS
from drivepulse_app.cars.trip_visuals import (
    _build_chart_widget,
    lift_dropdown_popover,
)
from drivepulse_app.common import _translate


def _build_trip_detail_widget(
    language: str,
    trip: Any,
    samples: list[Any],
    on_open_in_tour: Callable[[], None] | None = None,
) -> Gtk.Widget:
    """Stat-Karte + Datenverlauf für eine einzelne Fahrt.

    No map here on purpose — the route is shown only on the Tour map
    ("In Tour öffnen").

    *on_open_in_tour* adds an "In Tour öffnen" button under the stats; it is
    only passed for trips with a usable GPS track.
    """
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

    # Start and end timestamps duplicate what's already in the page heading
    # and the duration row below — drop both date stats here.
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

    if on_open_in_tour is not None:
        tour_btn = Gtk.Button(label=_translate(language, "cars.trip.open_in_tour"))
        tour_btn.add_css_class("suggested-action")
        tour_btn.set_halign(Gtk.Align.CENTER)
        tour_btn.connect("clicked", lambda _b: on_open_in_tour())
        outer.append(tour_btn)

    # --- Build per-metric point lists: (ts, value|None, lat, lon) ---
    # All samples count — without a map there is no need for a GPS fix, so
    # OBD-only trips (GPS never started) still get their charts.
    _base = list(samples)
    _gps_base = [s for s in samples if s["lat"] is not None and s["lon"] is not None]

    def _finite(v: Any) -> bool:
        """True only for finite, non-NaN numbers — rejects None, nan, inf, strings."""
        try:
            return math.isfinite(float(v))
        except (TypeError, ValueError):
            return False

    _min_valid = max(2, int(len(_base) * 0.30))  # mindestens 30 % der Samples
    metric_data: dict[str, list] = {}
    for _mk, _ml, _mu, _mc, _mf in _CHART_METRICS:
        _pts = [(s["ts"], s[_mk] if _finite(s[_mk]) else None, s["lat"], s["lon"])
                for s in _base]
        if sum(1 for p in _pts if p[1] is not None) >= _min_valid:
            metric_data[_mk] = _pts

    # Computed: cumulative Haversine distance along GPS track
    if len(_gps_base) >= 2:
        _cum_km = 0.0
        _elapsed_pts = []
        _prev_s = None
        for s in _gps_base:
            if _prev_s is not None:
                _dlat = math.radians(s["lat"] - _prev_s["lat"])
                _dlon = math.radians(s["lon"] - _prev_s["lon"])
                _a = (math.sin(_dlat / 2) ** 2 +
                      math.cos(math.radians(_prev_s["lat"])) *
                      math.cos(math.radians(s["lat"])) *
                      math.sin(_dlon / 2) ** 2)
                _cum_km += 6371.0 * 2 * math.atan2(math.sqrt(_a), math.sqrt(1 - _a))
            _elapsed_pts.append((s["ts"], _cum_km, s["lat"], s["lon"]))
            _prev_s = s
        if _cum_km > 0.01:
            metric_data["elapsed_km"] = _elapsed_pts

    _avail = [(k, _translate(language, lbl), u, c, f) for k, lbl, u, c, f in _CHART_METRICS if k in metric_data]
    if "elapsed_km" in metric_data:
        _avail.append((
            "elapsed_km",
            _translate(language, "cars.metric.elapsed_km"),
            "km",
            (0.20, 0.75, 0.60),
            "{:.2f}",
        ))

    _def_key = "speed_kmh" if "speed_kmh" in metric_data else (
        _avail[0][0] if _avail else None
    )
    def _elapsed_time_label(ts: float) -> str:
        return datetime.fromtimestamp(ts).strftime("%H:%M:%S")

    chart_state: dict[str, Any] = {}
    if _def_key:
        _dm = next(m for m in _avail if m[0] == _def_key)
        chart_state = {
            "pts": metric_data[_def_key],
            "unit": _dm[2],
            "color": _dm[3],
            "fmt": _dm[4],
            "key": _def_key,
        }
        if _def_key == "elapsed_km":
            chart_state["cursor_extra_fn"] = _elapsed_time_label

    # Shared cursor state: idx = index into chart_state["pts"], -1 = none
    cursor_state: dict[str, Any] = {"idx": -1}
    chart_area_ref: list[Any] = [None]

    def _on_cursor_change() -> None:
        if chart_area_ref[0]:
            chart_area_ref[0].queue_draw()

    # --- Datenverlauf ---
    if _avail and chart_state:
        if len(_avail) > 1:
            _str_model = Gtk.StringList()
            for _label in (m[1] for m in _avail):
                _str_model.append(_label)
            _dropdown = Gtk.DropDown.new(_str_model, None)
            _dropdown.set_halign(Gtk.Align.START)
            _dropdown.set_valign(Gtk.Align.CENTER)
            _dropdown.set_margin_bottom(5)
            _init_sel = next((i for i, m in enumerate(_avail) if m[0] == chart_state["key"]), 0)
            _dropdown.set_selected(_init_sel)

            def _on_metric_selected(dd: Gtk.DropDown, _pspec: Any, avail: list = _avail) -> None:
                sel = dd.get_selected()
                if 0 <= sel < len(avail):
                    key, _lbl, unit, color, fmt = avail[sel]
                    chart_state["pts"] = metric_data[key]
                    chart_state["unit"] = unit
                    chart_state["color"] = color
                    chart_state["fmt"] = fmt
                    chart_state["key"] = key
                    if key == "elapsed_km":
                        chart_state["cursor_extra_fn"] = _elapsed_time_label
                    else:
                        chart_state.pop("cursor_extra_fn", None)
                    cursor_state["idx"] = -1
                    if chart_area_ref[0]:
                        chart_area_ref[0].queue_draw()

            _dropdown.connect("notify::selected", _on_metric_selected)
            lift_dropdown_popover(_dropdown)
            outer.append(_dropdown)

        sp_area = _build_chart_widget(chart_state, cursor_state, _on_cursor_change)
        chart_area_ref[0] = sp_area
        outer.append(sp_area)

    if not _avail:
        empty = Gtk.Label(label=_translate(language, "cars.trip.no_data"), xalign=0.0)
        empty.add_css_class("dim-label")
        outer.append(empty)

    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    # Kinetic touch-scroll on the wrapper races with the chart's scrub
    # gesture for the same touch sequence — disable it so the chart wins
    # without needing to fight a parent claim race.
    scroll.set_kinetic_scrolling(False)
    scroll.set_vexpand(True)
    scroll.set_hexpand(True)
    scroll.set_child(outer)
    return scroll


def _safe_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
