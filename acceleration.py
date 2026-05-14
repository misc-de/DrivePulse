"""Acceleration measurement page for DrivePulse."""
from __future__ import annotations

import time
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from common import SOURCE_LANGUAGE, _make_label_responsive, _normalize_language, _translate

_WARNING_CSS = (
    b"button.warning-reset{background:rgba(229,165,10,0.85);color:#1c1c1c;}"
    b"button.warning-reset:hover{background:rgba(200,144,8,0.9);}"
)


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
        header_row.append(self.g_label)

        self.status_label = _make_label_responsive(Gtk.Label(label=""), 42)
        self.status_label.add_css_class("dim-label")
        self.status_label.set_halign(Gtk.Align.START)

        intro = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        intro.set_margin_bottom(10)
        intro.append(header_row)
        intro.append(self.status_label)

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

        self.append(intro)
        self.append(self.results_box)
        self.append(controls)

        self._refresh_texts()

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

        obd_caption = Gtk.Label()
        obd_caption.add_css_class("dim-label")
        obd_val = Gtk.Label(label="--")
        obd_val.set_width_chars(8)
        obd_val.set_xalign(1.0)
        obd_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        obd_box.append(obd_caption)
        obd_box.append(obd_val)
        obd_box.set_visible(False)

        gps_caption = Gtk.Label()
        gps_caption.add_css_class("dim-label")
        gps_val = Gtk.Label(label="--")
        gps_val.set_width_chars(8)
        gps_val.set_xalign(1.0)
        gps_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        gps_box.append(gps_caption)
        gps_box.append(gps_val)
        gps_box.set_visible(False)

        best_caption = Gtk.Label()
        best_caption.add_css_class("dim-label")
        best_val = Gtk.Label(label="--")
        best_val.add_css_class("title-4")
        best_val.set_width_chars(8)
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
        self._reset_labels()
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
        self.results = {target: {"obd": None, "gps": None} for target in self.SPEED_TARGETS_KMH}
        self.range_results = {r: {"obd": None, "gps": None} for r in self.RANGE_TARGETS_KMH}
        self._reset_labels()
        self._show_start()
        self._set_g_text(None)
        self.status_label.set_text(_translate(self.language, "acceleration.ready"))

    # ------------------------------------------------------------------
    # Data processing
    # ------------------------------------------------------------------

    def update_payload(self, payload: dict[str, Any], read_number: Callable[[dict[str, Any], str], float | None]) -> None:
        now = time.monotonic()
        obd_speed = read_number(payload, "speed")
        gps_speed = read_number(payload, "gps_speed")
        measured_g = read_number(payload, "acceleration_g")
        self._set_source_visibility(obd_speed is not None, gps_speed is not None)

        if obd_speed is not None and self.last_obd_speed is not None and self.last_speed_time is not None:
            dt = max(0.001, now - self.last_speed_time)
            acceleration_ms2 = ((obd_speed - self.last_obd_speed) / 3.6) / dt
            self.computed_acceleration_g = acceleration_ms2 / 9.80665

        if obd_speed is not None:
            self.last_obd_speed = obd_speed
            self.last_speed_time = now

        active_g = measured_g if measured_g is not None else self.computed_acceleration_g
        self._set_g_text(active_g)

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
