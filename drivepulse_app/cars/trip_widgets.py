"""Trip detail widgets for the Cars page."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from drivepulse_app.common import _translate


def _build_trip_detail_widget(
    language: str,
    trip: Any,
    on_open_in_tour: Callable[[], None] | None = None,
) -> Gtk.Widget:
    """Stat-Karte für eine einzelne Fahrt.

    No map and no chart here on purpose — route and data history are shown
    only on the Tour map ("In Tour öffnen").

    *on_open_in_tour* adds an "In Tour öffnen" button under the stats; it is
    only passed for trips with a usable GPS track — without it a short
    "no GPS data" note takes the button's place.
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
    else:
        # Same spot as the button, so a trip without GPS doesn't leave the
        # user wondering where "In Tour öffnen" went.
        no_gps = Gtk.Label(label=_translate(language, "cars.trip.no_gps"), xalign=0.5)
        no_gps.add_css_class("dim-label")
        no_gps.set_wrap(True)
        outer.append(no_gps)

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
    except (ValueError, TypeError):
        return None
