"""Acceleration measurement page for DrivePulse."""
from __future__ import annotations

import math
import time
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango  # noqa: E402

from common import SOURCE_LANGUAGE, _make_label_responsive, _normalize_language, _translate

_WARNING_CSS = (
    b"button.warning-reset{background:rgba(229,165,10,0.85);color:#1c1c1c;}"
    b"button.warning-reset:hover{background:rgba(200,144,8,0.9);}"
)


class GForceCanvas(Gtk.DrawingArea):
    """2D G-Force visualization: dot inside a ring grid (lateral × longitudinal).

    Style and scale follow the Sensor-Suite reference
    (https://github.com/misc-de/Sensor-Suite).
    """

    __gtype_name__ = "GForceCanvas"

    MAX_G = 2.0
    _SMOOTH = 0.30

    def __init__(self) -> None:
        super().__init__()
        self._target_x = 0.0
        self._target_y = 0.0
        self._target_z = 1.0
        self._x = 0.0
        self._y = 0.0
        self._z = 1.0
        self._has_data = False
        self.set_draw_func(self._draw)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_content_width(220)
        self.set_content_height(220)

    def update_g(self, x_g: float | None, y_g: float | None, z_g: float | None = None) -> None:
        if x_g is None and y_g is None and z_g is None:
            return
        if x_g is not None:
            self._target_x = float(x_g)
        if y_g is not None:
            self._target_y = float(y_g)
        if z_g is not None:
            self._target_z = float(z_g)
        self._has_data = True
        self._x += (self._target_x - self._x) * self._SMOOTH
        self._y += (self._target_y - self._y) * self._SMOOTH
        self._z += (self._target_z - self._z) * self._SMOOTH
        self.queue_draw()

    def clear(self) -> None:
        self._target_x = self._target_y = 0.0
        self._target_z = 1.0
        self._x = self._y = 0.0
        self._z = 1.0
        self._has_data = False
        self.queue_draw()

    @staticmethod
    def _text_center(cr: Any, text: str, x: float, y: float) -> None:
        ext = cr.text_extents(text)
        cr.move_to(x - ext.width / 2 - ext.x_bearing, y - ext.height / 2 - ext.y_bearing)
        cr.show_text(text)

    def _draw(self, _area: Gtk.DrawingArea, cr: Any, width: int, height: int) -> None:
        cx = width / 2
        cy = height / 2
        margin = min(width, height) * 0.165
        radius = min(width, height) / 2 - margin
        if radius < 18:
            return

        # Magnitude (deviation from 1g of gravity) drives colour
        mag = math.sqrt(self._x ** 2 + self._y ** 2 + self._z ** 2)
        dev = abs(mag - 1.0)
        if not self._has_data:
            r, g, b = 0.45, 0.48, 0.52
        elif dev < 0.10:
            r, g, b = 0.20, 0.78, 0.34
        elif dev < 0.45:
            r, g, b = 0.95, 0.72, 0.10
        else:
            r, g, b = 0.90, 0.22, 0.16

        font_value = max(11.0, radius * 0.16)
        font_ring  = max(8.0,  radius * 0.095)
        label_pad  = margin * 0.55

        # Background disc + outer ring
        cr.arc(cx, cy, radius, 0, math.tau)
        cr.set_source_rgba(0.08, 0.09, 0.11, 0.95)
        cr.fill_preserve()
        cr.set_source_rgba(r, g, b, 0.40)
        cr.set_line_width(2.2)
        cr.stroke()

        # Inner rings at 0.5g and 1.0g with labels
        for ring_g, alpha, lw in ((0.5, 0.20, 1.0), (1.0, 0.45, 1.4)):
            rpx = (ring_g / self.MAX_G) * radius
            cr.arc(cx, cy, rpx, 0, math.tau)
            cr.set_source_rgba(r, g, b, alpha)
            cr.set_line_width(lw)
            cr.stroke()
            cr.select_font_face("Cantarell", 0, 0)
            cr.set_font_size(font_ring)
            cr.set_source_rgba(0.60, 0.62, 0.66, 0.75)
            self._text_center(cr, f"{ring_g:.1f}g", cx + rpx * 0.70, cy - rpx * 0.70)

        # Cross-hairs through centre
        cr.set_line_width(1.0)
        cr.set_source_rgba(0.45, 0.47, 0.50, 0.40)
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            cr.move_to(cx + dx * radius * 0.06, cy + dy * radius * 0.06)
            cr.line_to(cx + dx * radius * 0.94, cy + dy * radius * 0.94)
        cr.stroke()

        # G-force dot (clamped to ring)
        dot_r = max(6.0, radius * 0.11)
        nx = self._x / self.MAX_G
        ny = self._y / self.MAX_G
        dist = math.sqrt(nx * nx + ny * ny)
        limit = 1.0 - dot_r / radius
        if dist > limit and dist > 0:
            nx *= limit / dist
            ny *= limit / dist
        # X axis: right = positive (right turn / right G)
        # Y axis: up = positive (forward acceleration)
        dot_x = cx + nx * radius
        dot_y = cy - ny * radius
        cr.arc(dot_x, dot_y, dot_r, 0, math.tau)
        cr.set_source_rgba(r, g, b, 0.30 if self._has_data else 0.18)
        cr.fill()
        cr.arc(dot_x, dot_y, dot_r, 0, math.tau)
        cr.set_source_rgba(r, g, b, 0.95 if self._has_data else 0.5)
        cr.set_line_width(2.2)
        cr.stroke()
        if self._has_data:
            cr.arc(dot_x, dot_y, dot_r * 0.32, 0, math.tau)
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.30)
            cr.fill()

        # Axis labels around the ring (top = longitudinal, right = lateral, bottom = magnitude)
        cr.select_font_face("Cantarell", 0, 0)
        cr.set_font_size(font_value)
        cr.set_source_rgba(0.92, 0.93, 0.95, 0.95 if self._has_data else 0.55)
        self._text_center(cr, f"{self._y:+.1f}g", cx, cy - radius - label_pad)
        self._text_center(cr, f"{self._x:+.1f}g", cx + radius + label_pad, cy)
        self._text_center(cr, f"{mag:.1f}g",       cx, cy + radius + label_pad)


def _apply_warning_css(button: Gtk.Button) -> None:
    """Apply amber/yellow styling to a button via the display CSS provider."""
    provider = Gtk.CssProvider()
    provider.load_from_data(_WARNING_CSS)
    button.add_css_class("warning-reset")

    def _on_realize(widget: Gtk.Widget) -> None:
        Gtk.StyleContext.add_provider_for_display(
            widget.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    button.connect("realize", _on_realize)


class AccelerationPage(Gtk.Box):
    __gtype_name__ = "AccelerationPage"

    SPEED_TARGETS_KMH = (30, 50, 70, 100, 150, 200)
    RANGE_TARGETS_KMH: tuple[tuple[int, int], ...] = ((100, 200),)
    G_FORCE_START_THRESHOLD = 0.1

    def __init__(self, language: str = SOURCE_LANGUAGE) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.language = _normalize_language(language)
        self.add_css_class("dp-accel-page")
        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.set_margin_start(12)
        self.set_margin_end(12)

        self.armed = False
        self.running = False
        self.start_monotonic: float | None = None
        self.last_obd_speed: float | None = None
        self.last_speed_time: float | None = None
        self.computed_acceleration_g: float | None = None
        self.max_obd_speed: float | None = None
        self.max_gps_speed: float | None = None
        self.max_g: float | None = None
        # Sticky availability flags — once seen during this measurement cycle,
        # the corresponding OBD/GPS column stays visible until reset to prevent
        # the row from flickering between sources on alternating payloads.
        self._obd_ever_seen: bool = False
        self._gps_ever_seen: bool = False
        # State for synthesizing lateral G from GPS heading change × speed
        self._last_heading_deg: float | None = None
        self._last_heading_time: float | None = None
        self._lateral_g: float = 0.0
        self.results: dict[int, dict[str, float | None]] = {
            target: {"obd": None, "gps": None} for target in self.SPEED_TARGETS_KMH
        }
        self.range_results: dict[tuple[int, int], dict[str, float | None]] = {
            r: {"obd": None, "gps": None} for r in self.RANGE_TARGETS_KMH
        }

        self.title_label = Gtk.Label()
        self.title_label.add_css_class("title-1")
        self.title_label.set_halign(Gtk.Align.START)

        self.g_label = Gtk.Label()
        self.g_label.add_css_class("title-2")
        self.g_label.set_halign(Gtk.Align.END)
        self.g_label.set_hexpand(True)

        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header_row.append(self.title_label)

        self.status_label = _make_label_responsive(Gtk.Label(label=""), 42)
        self.status_label.add_css_class("dim-label")
        self.status_label.set_halign(Gtk.Align.START)

        self.maxes_label = Gtk.Label()
        self.maxes_label.add_css_class("dim-label")
        self.maxes_label.set_halign(Gtk.Align.CENTER)
        self.maxes_label.set_hexpand(True)
        # Sit ~30 px below the time list (above the G-Force ball)
        self.maxes_label.set_margin_top(30)

        intro = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        intro.set_margin_bottom(10)
        intro.append(header_row)

        self.results_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.result_labels: dict[Any, Gtk.Label] = {}
        self.source_rows: dict[Any, Gtk.Box] = {}
        self._obd_captions: dict[Any, Gtk.Label] = {}
        self._gps_captions: dict[Any, Gtk.Label] = {}
        self._best_captions: dict[Any, Gtk.Label] = {}
        self._build_result_rows()

        self.start_button = Gtk.Button()
        self.start_button.add_css_class("suggested-action")
        self.start_button.add_css_class("pill")
        self.start_button.set_hexpand(True)
        self.start_button.connect("clicked", self.start_measurement)

        self.abort_button = Gtk.Button()
        self.abort_button.add_css_class("destructive-action")
        self.abort_button.add_css_class("pill")
        self.abort_button.set_hexpand(True)
        self.abort_button.set_visible(False)
        self.abort_button.connect("clicked", self.abort_measurement)

        self.reset_button = Gtk.Button()
        self.reset_button.add_css_class("pill")
        self.reset_button.set_hexpand(True)
        self.reset_button.connect("clicked", self.reset_measurement)
        _apply_warning_css(self.reset_button)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.set_margin_top(16)
        controls.set_hexpand(True)
        controls.append(self.start_button)
        controls.append(self.abort_button)
        controls.append(self.reset_button)

        # G-Force visualization (Sensor-Suite inspired) with Vmax/Gmax line above.
        # The canvas expands within its centred parent so it fills the available
        # space below the results list, while the inner draw routine still keeps
        # the circle square (uses min(width, height) for the radius).
        self.gforce_canvas = GForceCanvas()
        self.gforce_canvas.set_hexpand(True)
        self.gforce_canvas.set_vexpand(True)
        self.gforce_canvas.set_halign(Gtk.Align.FILL)
        self.gforce_canvas.set_valign(Gtk.Align.FILL)
        self.gforce_canvas.set_content_width(360)
        self.gforce_canvas.set_content_height(360)
        self.gforce_canvas.set_size_request(240, 240)

        self.gforce_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.gforce_box.set_hexpand(True)
        self.gforce_box.set_vexpand(True)
        # Group fills the remaining space so the canvas inside can grow. The
        # canvas keeps a square draw via min(w, h) in its _draw method, so
        # excess width in landscape just leaves padding either side.
        self.gforce_box.set_halign(Gtk.Align.FILL)
        self.gforce_box.set_valign(Gtk.Align.FILL)
        self.gforce_box.append(self.maxes_label)
        self.gforce_box.append(self.gforce_canvas)

        # Container that switches orientation between portrait (canvas below results)
        # and landscape (canvas next to results). results_box keeps its natural
        # height so the gforce group is free to centre in whatever is left.
        # Spacing 0 — the 30 px gap between list and Vmax/Gmax is enforced by
        # maxes_label's own margin_top.
        self.content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.content_box.set_hexpand(True)
        self.content_box.set_vexpand(True)
        self.results_box.set_hexpand(True)
        self.results_box.set_vexpand(False)
        self.results_box.set_valign(Gtk.Align.START)
        self.content_box.append(self.results_box)
        self.content_box.append(self.gforce_box)

        self.append(intro)
        self.append(self.content_box)
        self.append(controls)

        self._current_layout = "portrait"

        self._refresh_texts()

    # ------------------------------------------------------------------
    # Responsive layout — switch canvas position based on widget size
    # ------------------------------------------------------------------

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:  # type: ignore[override]
        Gtk.Box.do_size_allocate(self, width, height, baseline)
        # Landscape: canvas right of results. Portrait: canvas below results.
        # Threshold leaves a comfortable margin around square aspect.
        wants_landscape = width > height * 1.15 and width >= 560
        target = "landscape" if wants_landscape else "portrait"
        if target == self._current_layout:
            return
        self._current_layout = target
        if target == "landscape":
            self.content_box.set_orientation(Gtk.Orientation.HORIZONTAL)
            self.content_box.set_spacing(16)
        else:
            self.content_box.set_orientation(Gtk.Orientation.VERTICAL)
            self.content_box.set_spacing(8)

    # ------------------------------------------------------------------
    # Layout helpers
    # ------------------------------------------------------------------

    def _build_result_rows(self) -> None:
        for target in self.SPEED_TARGETS_KMH:
            self.results_box.append(self._make_result_row(f"0–{target} km/h", target))
        for lo, hi in self.RANGE_TARGETS_KMH:
            self.results_box.append(self._make_result_row(f"{lo}–{hi} km/h", (lo, hi)))

    def _make_result_row(self, label_text: str, key: Any) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.add_css_class("card")
        row.set_margin_top(2)
        row.set_margin_bottom(2)

        name_lbl = Gtk.Label(label=label_text)
        name_lbl.add_css_class("heading")
        name_lbl.set_halign(Gtk.Align.START)
        name_lbl.set_hexpand(True)
        name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        name_lbl.set_max_width_chars(12)

        obd_caption = Gtk.Label()
        obd_caption.add_css_class("dim-label")
        obd_val = Gtk.Label(label="--")
        obd_val.set_width_chars(6)
        obd_val.set_xalign(1.0)
        obd_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        obd_box.append(obd_caption)
        obd_box.append(obd_val)
        obd_box.set_visible(False)

        gps_caption = Gtk.Label()
        gps_caption.add_css_class("dim-label")
        gps_val = Gtk.Label(label="--")
        gps_val.set_width_chars(6)
        gps_val.set_xalign(1.0)
        gps_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        gps_box.append(gps_caption)
        gps_box.append(gps_val)
        gps_box.set_visible(False)

        best_caption = Gtk.Label()
        best_caption.add_css_class("dim-label")
        best_val = Gtk.Label(label="--")
        best_val.add_css_class("title-4")
        best_val.set_width_chars(7)
        best_val.set_xalign(1.0)

        row.append(name_lbl)
        row.append(obd_box)
        row.append(gps_box)
        row.append(best_caption)
        row.append(best_val)

        self.result_labels[(key, "obd")] = obd_val
        self.result_labels[(key, "gps")] = gps_val
        self.result_labels[(key, "best")] = best_val
        self.source_rows[(key, "obd")] = obd_box
        self.source_rows[(key, "gps")] = gps_box
        self._obd_captions[key] = obd_caption
        self._gps_captions[key] = gps_caption
        self._best_captions[key] = best_caption
        return row

    def _all_keys(self) -> list[Any]:
        return list(self.SPEED_TARGETS_KMH) + list(self.RANGE_TARGETS_KMH)

    # ------------------------------------------------------------------
    # Text / language
    # ------------------------------------------------------------------

    def _refresh_texts(self) -> None:
        self.title_label.set_text(_translate(self.language, "acceleration.title"))
        self.start_button.set_label(_translate(self.language, "acceleration.start"))
        self.abort_button.set_label(_translate(self.language, "acceleration.abort"))
        self.reset_button.set_label(_translate(self.language, "acceleration.reset"))
        if not self.armed and not self.running:
            self.status_label.set_text(_translate(self.language, "acceleration.ready"))
        obd_text = _translate(self.language, "acceleration.obd")
        gps_text = _translate(self.language, "acceleration.gps")
        best_text = _translate(self.language, "acceleration.best")
        for key in self._all_keys():
            self._obd_captions[key].set_text(obd_text)
            self._gps_captions[key].set_text(gps_text)
            self._best_captions[key].set_text(best_text)
        self._update_best_labels()
        self._update_maxes_label()

    def set_language(self, language: str) -> None:
        self.language = _normalize_language(language)
        self._refresh_texts()

    def set_theme(self, theme_id: str) -> None:
        for cls in list(self.get_css_classes()):
            if cls.startswith("dp-accel-theme-"):
                self.remove_css_class(cls)
        safe = theme_id.replace(":", "-").replace("_", "-")
        self.add_css_class(f"dp-accel-theme-{safe}")

    def _set_g_text(self, active_g: float | None) -> None:
        if active_g is None:
            self.g_label.set_text(_translate(self.language, "acceleration.g.empty"))
        else:
            self.g_label.set_text(_translate(self.language, "acceleration.g", value=f"{active_g:.3f}"))

    def _update_maxes_label(self) -> None:
        vmax = self.max_obd_speed if self.max_obd_speed is not None else self.max_gps_speed
        parts: list[str] = []
        if vmax is not None:
            parts.append(_translate(self.language, "acceleration.vmax", value=f"{vmax:.0f} km/h"))
        else:
            parts.append(_translate(self.language, "acceleration.vmax.empty"))
        if self.max_g is not None:
            parts.append(_translate(self.language, "acceleration.gmax", value=f"{self.max_g:.1f}"))
        else:
            parts.append(_translate(self.language, "acceleration.gmax.empty"))
        self.maxes_label.set_text("  ·  ".join(parts))

    # ------------------------------------------------------------------
    # State updates
    # ------------------------------------------------------------------

    def _update_best_labels(self) -> None:
        for target in self.SPEED_TARGETS_KMH:
            values = self.results[target]
            measured = [v for v in values.values() if v is not None]
            best = min(measured) if measured else None
            self.result_labels[(target, "best")].set_text("--" if best is None else f"{best:.2f} s")
        for rkey in self.RANGE_TARGETS_KMH:
            values = self.range_results[rkey]
            measured = [v for v in values.values() if v is not None]
            best = min(measured) if measured else None
            self.result_labels[(rkey, "best")].set_text("--" if best is None else f"{best:.2f} s")

    def _set_source_visibility(self, obd_available: bool, gps_available: bool) -> None:
        for key in self._all_keys():
            if isinstance(key, tuple):
                obd_has = self.range_results[key]["obd"] is not None
                gps_has = self.range_results[key]["gps"] is not None
            else:
                obd_has = self.results[key]["obd"] is not None
                gps_has = self.results[key]["gps"] is not None
            self.source_rows[(key, "obd")].set_visible(obd_available or obd_has)
            self.source_rows[(key, "gps")].set_visible(gps_available or gps_has)

    def _is_active(self) -> bool:
        """Live updates only happen while a measurement is running or armed.
        After abort/done the maxes label and source rows freeze on screen."""
        return self.armed or self.running

    def _reset_labels(self) -> None:
        for key in self._all_keys():
            for source in ("obd", "gps", "best"):
                self.result_labels[(key, source)].set_text("--")

    def _show_start(self) -> None:
        self.start_button.set_visible(True)
        self.abort_button.set_visible(False)

    def _show_abort(self) -> None:
        self.start_button.set_visible(False)
        self.abort_button.set_visible(True)

    # ------------------------------------------------------------------
    # Button callbacks
    # ------------------------------------------------------------------

    def start_measurement(self, *_args: Any) -> None:
        self.armed = True
        self.running = False
        self.start_monotonic = None
        self.results = {target: {"obd": None, "gps": None} for target in self.SPEED_TARGETS_KMH}
        self.range_results = {r: {"obd": None, "gps": None} for r in self.RANGE_TARGETS_KMH}
        self.max_obd_speed = None
        self.max_gps_speed = None
        self.max_g = None
        self._obd_ever_seen = False
        self._gps_ever_seen = False
        self._reset_labels()
        self._set_source_visibility(False, False)
        self._update_maxes_label()
        self._show_abort()
        self.status_label.set_text(_translate(self.language, "acceleration.armed"))

    def abort_measurement(self, *_args: Any) -> None:
        self.armed = False
        self.running = False
        self._show_start()
        self.status_label.set_text(_translate(self.language, "acceleration.done"))

    def reset_measurement(self, *_args: Any) -> None:
        self.armed = False
        self.running = False
        self.start_monotonic = None
        self.last_obd_speed = None
        self.last_speed_time = None
        self.computed_acceleration_g = None
        self.max_obd_speed = None
        self.max_gps_speed = None
        self.max_g = None
        self._obd_ever_seen = False
        self._gps_ever_seen = False
        self._last_heading_deg = None
        self._last_heading_time = None
        self._lateral_g = 0.0
        self.gforce_canvas.clear()
        self.results = {target: {"obd": None, "gps": None} for target in self.SPEED_TARGETS_KMH}
        self.range_results = {r: {"obd": None, "gps": None} for r in self.RANGE_TARGETS_KMH}
        self._reset_labels()
        self._set_source_visibility(False, False)
        self._update_maxes_label()
        self._show_start()
        self._set_g_text(None)
        self.status_label.set_text(_translate(self.language, "acceleration.ready"))

    def update_gforce_raw(self, x_g: float, y_g: float, z_g: float) -> None:
        """Feed raw physical accelerometer values (in g) to the G-Force canvas."""
        self.gforce_canvas.update_g(x_g, y_g, z_g)

    # ------------------------------------------------------------------
    # Data processing
    # ------------------------------------------------------------------

    def _update_lateral_g(self, heading_deg: float | None, speed_kmh: float | None, now: float) -> None:
        """Estimate lateral G from GPS heading change × speed (centripetal acceleration).

        a_lat = v · ω, where ω = d(heading)/dt. Below ~10 km/h GPS heading is too
        noisy, so the estimate falls back to 0 to avoid jitter in the display.
        """
        if heading_deg is None or speed_kmh is None or speed_kmh < 10.0:
            self._last_heading_deg = heading_deg
            self._last_heading_time = now
            self._lateral_g *= 0.6  # decay toward zero when no usable input
            return
        if self._last_heading_deg is None or self._last_heading_time is None:
            self._last_heading_deg = heading_deg
            self._last_heading_time = now
            return
        dt = max(0.05, now - self._last_heading_time)
        # Wrap heading delta into (-180, 180]
        delta = (heading_deg - self._last_heading_deg + 540.0) % 360.0 - 180.0
        omega_rad_s = math.radians(delta) / dt
        v_ms = speed_kmh / 3.6
        a_lat_ms2 = v_ms * omega_rad_s
        # Light low-pass to suppress GPS jitter; positive = right turn
        target = a_lat_ms2 / 9.80665
        self._lateral_g += (target - self._lateral_g) * 0.35
        self._last_heading_deg = heading_deg
        self._last_heading_time = now

    def update_payload(self, payload: dict[str, Any], read_number: Callable[[dict[str, Any], str], float | None]) -> None:
        now = time.monotonic()
        obd_speed = read_number(payload, "speed")
        gps_speed = read_number(payload, "gps_speed")
        measured_g = read_number(payload, "acceleration_g")
        heading = read_number(payload, "gps_heading")
        active = self._is_active()

        self._update_lateral_g(heading, gps_speed if gps_speed is not None else obd_speed, now)

        if obd_speed is not None and self.last_obd_speed is not None and self.last_speed_time is not None:
            dt = max(0.001, now - self.last_speed_time)
            acceleration_ms2 = ((obd_speed - self.last_obd_speed) / 3.6) / dt
            self.computed_acceleration_g = acceleration_ms2 / 9.80665

        if obd_speed is not None:
            self.last_obd_speed = obd_speed
            self.last_speed_time = now

        # Live displays (current G, gforce ball) keep updating regardless of
        # measurement state — they show "right now", not measurement data.
        active_g = measured_g if measured_g is not None else self.computed_acceleration_g
        self._set_g_text(active_g)
        if active_g is not None or gps_speed is not None or obd_speed is not None:
            # Y axis: longitudinal G (positive = forward acceleration)
            # X axis: lateral G (positive = right turn, computed via heading delta)
            self.gforce_canvas.update_g(self._lateral_g, active_g if active_g is not None else 0.0, 1.0)

        # Measurement-bound displays (source row visibility, Vmax/Gmax) only
        # change while a run is armed/active. Once it ends, they freeze at their
        # final value and stop flickering between OBD/GPS payloads.
        if not active:
            return

        # Sticky source visibility: a column appears as soon as that source has
        # ever produced a speed during this measurement cycle, and stays put
        # until reset, so alternating GPS/OBD payloads no longer cause flicker.
        if obd_speed is not None:
            self._obd_ever_seen = True
        if gps_speed is not None:
            self._gps_ever_seen = True
        self._set_source_visibility(self._obd_ever_seen, self._gps_ever_seen)

        # Maxima fortschreiben — nur während laufender / scharfer Messung
        if obd_speed is not None and (self.max_obd_speed is None or obd_speed > self.max_obd_speed):
            self.max_obd_speed = obd_speed
        if gps_speed is not None and (self.max_gps_speed is None or gps_speed > self.max_gps_speed):
            self.max_gps_speed = gps_speed
        if active_g is not None and (self.max_g is None or active_g > self.max_g):
            self.max_g = active_g
        self._update_maxes_label()

        if self.armed and not self.running:
            speed_rising = self.computed_acceleration_g is not None and self.computed_acceleration_g > self.G_FORCE_START_THRESHOLD
            g_rising = active_g is not None and active_g > self.G_FORCE_START_THRESHOLD
            if speed_rising or g_rising:
                self.running = True
                self.start_monotonic = now
                self.status_label.set_text(_translate(self.language, "acceleration.running"))

        if not self.running or self.start_monotonic is None:
            return

        elapsed = now - self.start_monotonic
        for target in self.SPEED_TARGETS_KMH:
            row = self.results[target]
            if row["obd"] is None and obd_speed is not None and obd_speed >= target:
                row["obd"] = elapsed
                self.result_labels[(target, "obd")].set_text(f"{elapsed:.2f} s")
            if row["gps"] is None and gps_speed is not None and gps_speed >= target:
                row["gps"] = elapsed
                self.result_labels[(target, "gps")].set_text(f"{elapsed:.2f} s")

        for lo, hi in self.RANGE_TARGETS_KMH:
            rrow = self.range_results[(lo, hi)]
            lo_obd = self.results.get(lo, {}).get("obd")
            hi_obd = self.results.get(hi, {}).get("obd")
            if rrow["obd"] is None and lo_obd is not None and hi_obd is not None:
                rrow["obd"] = hi_obd - lo_obd
                self.result_labels[((lo, hi), "obd")].set_text(f"{rrow['obd']:.2f} s")
            lo_gps = self.results.get(lo, {}).get("gps")
            hi_gps = self.results.get(hi, {}).get("gps")
            if rrow["gps"] is None and lo_gps is not None and hi_gps is not None:
                rrow["gps"] = hi_gps - lo_gps
                self.result_labels[((lo, hi), "gps")].set_text(f"{rrow['gps']:.2f} s")

        self._update_best_labels()

        all_done = all(v["obd"] is not None or v["gps"] is not None for v in self.results.values())
        if all_done:
            self.running = False
            self.armed = False
            self._show_start()
            self.status_label.set_text(_translate(self.language, "acceleration.done"))
