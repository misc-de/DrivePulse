"""Acceleration measurement page for DrivePulse."""
from __future__ import annotations

import math
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango  # noqa: E402

from .acceleration_canvas import GForceCanvas
from .acceleration_processing import AccelerationProcessingMixin
from .acceleration_replay import AccelerationReplayMixin
from .common import SOURCE_LANGUAGE, _make_label_responsive, _normalize_language, _translate

_WARNING_CSS = (
    b"button.warning-reset{background:rgba(229,165,10,0.85);color:#1c1c1c;}"
    b"button.warning-reset:hover{background:rgba(200,144,8,0.9);}"
)


class _NoopSizeGroup:
    def add_widget(self, _widget: Gtk.Widget) -> None:
        pass


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


class AccelerationPage(AccelerationProcessingMixin, AccelerationReplayMixin, Gtk.Box):
    __gtype_name__ = "AccelerationPage"

    SPEED_TARGETS_KMH = (30, 50, 70, 100, 150, 200)
    RANGE_TARGETS_KMH: tuple[tuple[int, int], ...] = ((100, 200),)
    G_ENGAGE_THRESHOLD   = 0.20   # must sustain for confirm window
    G_PRESTART_THRESHOLD = 0.06   # retroactive start crossover
    G_CONFIRM_WINDOW     = 0.150  # seconds the engage threshold must be held
    G_MIN_SPEED_KMH      = 1.0   # speed gate to confirm real start

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
        self._max_obd_speed_t: float | None = None
        self._max_gps_speed_t: float | None = None
        self._saved_vmax_obd: float | None = None
        self._saved_vmax_obd_t: float | None = None
        self._saved_vmax_gps: float | None = None
        self._saved_vmax_gps_t: float | None = None
        self._vmax_name_lbl: Any = None
        self._gforce_trigger: bool = False
        self._raw_g_dev: float = 0.0
        self._engage_since:    float | None = None
        self._prestart_since:  float | None = None
        self._engage_threshold: float = self.G_ENGAGE_THRESHOLD
        self.on_engage_threshold_changed: Callable[[float], None] | None = None
        # Sticky availability flags — once seen during this measurement cycle,
        # the corresponding OBD/GPS column stays visible until reset to prevent
        # the row from flickering between sources on alternating payloads.
        self._obd_ever_seen: bool = False
        self._gps_ever_seen: bool = False
        # State for synthesizing lateral G from GPS heading change × speed
        self._last_heading_deg: float | None = None
        self._last_heading_time: float | None = None
        self._lateral_g: float = 0.0
        self.on_mock_start: Callable[[], None] | None = None
        self.on_run_complete: Callable[[dict, list], None] | None = None
        self._run_samples: list[tuple[float, float | None, float]] = []  # (elapsed, active_g, lateral_g)
        self._saved_results: dict | None = None
        self._saved_range_results: dict | None = None
        self._replay_active: bool = False
        self._replay_start_mono: float = 0.0
        self._replay_timer_id: int | None = None
        self._replay_sample_idx: int = 0
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
        self.maxes_label.add_css_class("title-2")
        self.maxes_label.set_halign(Gtk.Align.CENTER)
        self.maxes_label.set_hexpand(True)
        self.maxes_label.set_wrap(True)
        justification = getattr(getattr(Gtk, "Justification", None), "CENTER", None)
        if justification is not None:
            self.maxes_label.set_justify(justification)
        self.maxes_label.set_margin_top(24)

        intro = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        intro.set_margin_bottom(22)
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

        self.replay_button = Gtk.Button()
        self.replay_button.add_css_class("suggested-action")
        self.replay_button.add_css_class("pill")
        self.replay_button.set_hexpand(True)
        self.replay_button.set_visible(False)
        self.replay_button.connect("clicked", self.replay_measurement)

        self.gforce_trigger_check = Gtk.CheckButton()
        self.gforce_trigger_check.connect("toggled", self._on_gforce_trigger_toggled)

        self._threshold_minus = Gtk.Button(label="−")
        self._threshold_minus.add_css_class("flat")
        self._threshold_minus.add_css_class("circular")
        self._threshold_minus.connect("clicked", self._on_threshold_minus)

        self._threshold_label = Gtk.Label(label=f"{self._engage_threshold:.2f} g")
        self._threshold_label.set_width_chars(6)

        self._threshold_plus = Gtk.Button(label="+")
        self._threshold_plus.add_css_class("flat")
        self._threshold_plus.add_css_class("circular")
        self._threshold_plus.connect("clicked", self._on_threshold_plus)

        _threshold_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        _threshold_controls.set_halign(Gtk.Align.CENTER)
        _threshold_controls.append(self._threshold_minus)
        _threshold_controls.append(self._threshold_label)
        _threshold_controls.append(self._threshold_plus)

        self._trigger_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._trigger_row.set_halign(Gtk.Align.FILL)
        self.gforce_trigger_check.set_halign(Gtk.Align.CENTER)
        self._trigger_row.append(self.gforce_trigger_check)
        self._trigger_row.append(_threshold_controls)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.set_margin_top(8)
        controls.set_margin_bottom(20)
        controls.set_hexpand(True)
        controls.append(self.start_button)
        controls.append(self.abort_button)
        controls.append(self.replay_button)
        controls.append(self.reset_button)

        # GForce canvas — fills right column (landscape) or lower half (portrait).
        self.gforce_canvas = GForceCanvas()
        self.gforce_canvas.set_hexpand(True)
        self.gforce_canvas.set_vexpand(True)
        self.gforce_canvas.set_halign(Gtk.Align.FILL)
        self.gforce_canvas.set_valign(Gtk.Align.FILL)
        self.gforce_canvas.set_size_request(-1, 200)

        self.gforce_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.gforce_box.set_hexpand(True)
        self.gforce_box.set_vexpand(True)
        self.gforce_box.set_halign(Gtk.Align.FILL)
        self.gforce_box.set_valign(Gtk.Align.FILL)
        self.gforce_box.append(self.maxes_label)
        self.gforce_box.append(self.gforce_canvas)

        # Results table in a scroll window so it never overflows in landscape.
        self.results_box.set_hexpand(True)
        self.results_box.set_vexpand(False)
        self.results_box.set_valign(Gtk.Align.START)
        self.results_scroll = Gtk.ScrolledWindow()
        self.results_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.results_scroll.set_propagate_natural_height(True)
        self.results_scroll.set_hexpand(True)
        self.results_scroll.set_vexpand(False)
        self.results_scroll.set_valign(Gtk.Align.START)
        self.results_scroll.set_child(self.results_box)

        # content_box: left = results, right = gforce (landscape) / top+bottom (portrait).
        self.content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.content_box.set_hexpand(True)
        self.content_box.set_vexpand(True)
        self.content_box.append(self.results_scroll)
        self.content_box.append(self.gforce_box)

        # _trigger_row and controls always span the full width below content_box.
        self._trigger_row.set_margin_top(8)

        self.append(intro)
        self.append(self.content_box)
        self.append(self._trigger_row)
        self.append(controls)

        self._current_layout = "portrait"
        self._device_rotation = 0

        self._refresh_texts()

    # ------------------------------------------------------------------
    # Responsive layout — 50/50 split in landscape, stacked in portrait
    # ------------------------------------------------------------------

    def set_device_rotation(self, angle: int) -> None:
        """Called by the window when the physical device orientation changes."""
        self._device_rotation = angle % 360
        queue_allocate = getattr(self, "queue_allocate", None)
        if callable(queue_allocate):
            queue_allocate()

    def _layout_target_for_size(self, width: int, height: int) -> str:
        # Match DashboardLayoutMixin: on Phosh/compositor-side rotation the GTK
        # surface often stays portrait while the compositor rotates it physically.
        # In that case the GTK portrait layout appears landscape on the device.
        if self._device_rotation in (90, 270) and width < height:
            return "portrait"
        return "landscape" if width >= height else "portrait"

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:  # type: ignore[override]
        Gtk.Box.do_size_allocate(self, width, height, baseline)
        target = self._layout_target_for_size(width, height)
        if target == self._current_layout:
            return
        self._current_layout = target
        if target == "landscape":
            self.content_box.set_orientation(Gtk.Orientation.HORIZONTAL)
            self.content_box.set_spacing(12)
            self.content_box.set_homogeneous(True)
            # Left column: table scrolls vertically, fills its half
            self.results_scroll.set_vexpand(True)
            self.results_scroll.set_valign(Gtk.Align.FILL)
            self.results_scroll.set_propagate_natural_height(False)
            self.maxes_label.set_margin_top(8)
        else:
            self.content_box.set_orientation(Gtk.Orientation.VERTICAL)
            self.content_box.set_spacing(0)
            self.content_box.set_homogeneous(False)
            # Table sits compactly at top, canvas expands below
            self.results_scroll.set_vexpand(False)
            self.results_scroll.set_valign(Gtk.Align.START)
            self.results_scroll.set_propagate_natural_height(True)
            self.maxes_label.set_margin_top(24)

    # ------------------------------------------------------------------
    # Layout helpers
    # ------------------------------------------------------------------

    def _build_result_rows(self) -> None:
        # SizeGroups ensure value columns line up across all rows (no Grid needed)
        self._col_obd_sg = self._make_size_group()
        self._col_gps_sg = self._make_size_group()
        self._col_best_sg = self._make_size_group()

        self.results_box.append(self._make_header_row())
        for target in self.SPEED_TARGETS_KMH:
            self.results_box.append(self._make_result_row(f"0–{target} km/h", target))
        for lo, hi in self.RANGE_TARGETS_KMH:
            self.results_box.append(self._make_result_row(f"{lo}–{hi} km/h", (lo, hi)))
        self.results_box.append(self._make_result_row("Vmax", "vmax"))

    def _make_size_group(self) -> Any:
        size_group = getattr(Gtk, "SizeGroup", None)
        size_group_mode = getattr(getattr(Gtk, "SizeGroupMode", None), "HORIZONTAL", None)
        if size_group is None or size_group_mode is None:
            return _NoopSizeGroup()
        return size_group(mode=size_group_mode)

    def _make_header_row(self) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        row.set_margin_top(4)
        row.set_margin_bottom(2)
        row.set_margin_start(4)
        row.set_margin_end(4)

        self._header_name_lbl = Gtk.Label()
        self._header_name_lbl.add_css_class("caption-heading")
        self._header_name_lbl.add_css_class("dim-label")
        self._header_name_lbl.set_halign(Gtk.Align.START)
        self._header_name_lbl.set_hexpand(True)

        self._header_obd_lbl = Gtk.Label()
        self._header_obd_lbl.add_css_class("caption-heading")
        self._header_obd_lbl.add_css_class("dim-label")
        self._header_obd_lbl.set_xalign(1.0)
        self._header_obd_lbl.set_visible(False)
        self._col_obd_sg.add_widget(self._header_obd_lbl)

        self._header_gps_lbl = Gtk.Label()
        self._header_gps_lbl.add_css_class("caption-heading")
        self._header_gps_lbl.add_css_class("dim-label")
        self._header_gps_lbl.set_xalign(1.0)
        self._header_gps_lbl.set_visible(False)
        self._col_gps_sg.add_widget(self._header_gps_lbl)

        self._header_best_lbl = Gtk.Label()
        self._header_best_lbl.add_css_class("caption-heading")
        self._header_best_lbl.add_css_class("dim-label")
        self._header_best_lbl.set_xalign(1.0)
        self._col_best_sg.add_widget(self._header_best_lbl)

        row.append(self._header_name_lbl)
        row.append(self._header_obd_lbl)
        row.append(self._header_gps_lbl)
        row.append(self._header_best_lbl)
        return row

    def _make_result_row(self, label_text: str, key: Any) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        row.add_css_class("card")
        row.set_margin_top(2)
        row.set_margin_bottom(2)

        name_lbl = Gtk.Label(label=label_text)
        name_lbl.add_css_class("heading")
        name_lbl.set_halign(Gtk.Align.START)
        name_lbl.set_hexpand(True)
        ellipsize_mode = getattr(getattr(Pango, "EllipsizeMode", None), "END", None)
        if ellipsize_mode is not None:
            name_lbl.set_ellipsize(ellipsize_mode)
        name_lbl.set_max_width_chars(10)

        obd_val = Gtk.Label(label="--")
        obd_val.add_css_class("monospace")
        obd_val.set_width_chars(6)
        obd_val.set_xalign(1.0)
        obd_val.set_visible(False)
        self._col_obd_sg.add_widget(obd_val)

        gps_val = Gtk.Label(label="--")
        gps_val.add_css_class("monospace")
        gps_val.set_width_chars(6)
        gps_val.set_xalign(1.0)
        gps_val.set_visible(False)
        self._col_gps_sg.add_widget(gps_val)

        best_val = Gtk.Label(label="--")
        best_val.add_css_class("monospace")
        best_val.add_css_class("heading")
        best_val.set_width_chars(6)
        best_val.set_xalign(1.0)
        self._col_best_sg.add_widget(best_val)

        row.append(name_lbl)
        row.append(obd_val)
        row.append(gps_val)
        row.append(best_val)

        self.result_labels[(key, "obd")] = obd_val
        self.result_labels[(key, "gps")] = gps_val
        self.result_labels[(key, "best")] = best_val
        self.source_rows[(key, "obd")] = obd_val
        self.source_rows[(key, "gps")] = gps_val
        if key == "vmax":
            self._vmax_name_lbl = name_lbl
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
        self.gforce_trigger_check.set_label(_translate(self.language, "acceleration.gforce_trigger"))
        if self.replay_button.get_visible() and not self._replay_active:
            self.replay_button.set_label(_translate(self.language, "acceleration.replay"))
        if not self.armed and not self.running:
            self.status_label.set_text(_translate(self.language, "acceleration.ready"))
        if hasattr(self, "_header_name_lbl"):
            self._header_name_lbl.set_text(_translate(self.language, "acceleration.title"))
        if hasattr(self, "_header_obd_lbl"):
            self._header_obd_lbl.set_text(_translate(self.language, "acceleration.obd"))
        if hasattr(self, "_header_gps_lbl"):
            self._header_gps_lbl.set_text(_translate(self.language, "acceleration.gps"))
        if hasattr(self, "_header_best_lbl"):
            self._header_best_lbl.set_text(_translate(self.language, "acceleration.best"))
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

    def _update_vmax_row(
        self,
        obd_v: float | None = None, obd_t: float | None = None,
        gps_v: float | None = None, gps_t: float | None = None,
    ) -> None:
        obd_lbl = self.result_labels.get(("vmax", "obd"))
        gps_lbl = self.result_labels.get(("vmax", "gps"))
        best_lbl = self.result_labels.get(("vmax", "best"))
        obd_box = self.source_rows.get(("vmax", "obd"))
        gps_box = self.source_rows.get(("vmax", "gps"))

        if obd_box:
            obd_box.set_visible(self._obd_ever_seen or obd_t is not None)
        if gps_box:
            gps_box.set_visible(self._gps_ever_seen or gps_t is not None)

        # Name label shows the highest max speed reached so far
        if self._vmax_name_lbl is not None:
            speeds = [v for v in [obd_v, gps_v] if v is not None]
            if speeds:
                self._vmax_name_lbl.set_text(f"Vmax {max(speeds):.0f} km/h")
            else:
                self._vmax_name_lbl.set_text("Vmax")

        # OBD/GPS columns show the elapsed time when the peak was hit — same format as other rows
        if obd_lbl:
            obd_lbl.set_text(f"{obd_t:.2f} s" if obd_t is not None else "--")
        if gps_lbl:
            gps_lbl.set_text(f"{gps_t:.2f} s" if gps_t is not None else "--")

        # Ø = average of available times (same as other rows)
        if best_lbl:
            times = [t for t in [obd_t, gps_t] if t is not None]
            avg = sum(times) / len(times) if times else None
            best_lbl.set_text(f"{avg:.2f} s" if avg is not None else "--")

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
            measured = [v for v in self.results[target].values() if v is not None]
            avg = sum(measured) / len(measured) if measured else None
            self.result_labels[(target, "best")].set_text("--" if avg is None else f"{avg:.2f} s")
        for rkey in self.RANGE_TARGETS_KMH:
            measured = [v for v in self.range_results[rkey].values() if v is not None]
            avg = sum(measured) / len(measured) if measured else None
            self.result_labels[(rkey, "best")].set_text("--" if avg is None else f"{avg:.2f} s")

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
        if hasattr(self, "_header_obd_lbl"):
            self._header_obd_lbl.set_visible(obd_available)
        if hasattr(self, "_header_gps_lbl"):
            self._header_gps_lbl.set_visible(gps_available)

    def _is_active(self) -> bool:
        """Live updates only happen while a measurement is running or armed.
        After abort/done the maxes label and source rows freeze on screen."""
        return self.armed or self.running

    def _reset_labels(self) -> None:
        for key in self._all_keys():
            for source in ("obd", "gps", "best"):
                self.result_labels[(key, source)].set_text("--")
        for source in ("obd", "gps", "best"):
            if ("vmax", source) in self.result_labels:
                self.result_labels[("vmax", source)].set_text("--")
        if self._vmax_name_lbl is not None:
            self._vmax_name_lbl.set_text("Vmax")

    def _show_start(self) -> None:
        self.start_button.set_visible(True)
        self.abort_button.set_visible(False)
        self.replay_button.set_visible(False)

    def _show_abort(self) -> None:
        self.start_button.set_visible(False)
        self.abort_button.set_visible(True)
        self.replay_button.set_visible(False)

    def _show_replay(self) -> None:
        self.start_button.set_visible(False)
        self.abort_button.set_visible(False)
        self.replay_button.set_label(_translate(self.language, "acceleration.replay"))
        self.replay_button.set_visible(True)

    # ------------------------------------------------------------------
    # Button callbacks
    # ------------------------------------------------------------------

    def start_measurement(self, *_args: Any) -> None:
        self._stop_replay()
        self.armed = True
        self.running = False
        self.start_monotonic = None
        self.results = {target: {"obd": None, "gps": None} for target in self.SPEED_TARGETS_KMH}
        self.range_results = {r: {"obd": None, "gps": None} for r in self.RANGE_TARGETS_KMH}
        self.max_obd_speed = None
        self.max_gps_speed = None
        self.max_g = None
        self._max_obd_speed_t = None
        self._max_gps_speed_t = None
        self._obd_ever_seen = False
        self._gps_ever_seen = False
        self._engage_since   = None
        self._prestart_since = None
        self._run_samples = []
        self._saved_results = None
        self._saved_range_results = None
        self._reset_labels()
        self._set_source_visibility(False, False)
        self._update_maxes_label()
        self._show_abort()
        self.status_label.set_text(_translate(self.language, "acceleration.armed"))
        if self.on_mock_start is not None:
            self.on_mock_start()

    def abort_measurement(self, *_args: Any) -> None:
        self.armed = False
        self.running = False
        self._show_start()
        self.status_label.set_text(_translate(self.language, "acceleration.done"))

    def reset_measurement(self, *_args: Any) -> None:
        self._stop_replay()
        self.armed = False
        self.running = False
        self.start_monotonic = None
        self.last_obd_speed = None
        self.last_speed_time = None
        self.computed_acceleration_g = None
        self.max_obd_speed = None
        self.max_gps_speed = None
        self.max_g = None
        self._max_obd_speed_t = None
        self._max_gps_speed_t = None
        self._obd_ever_seen = False
        self._gps_ever_seen = False
        self._last_heading_deg = None
        self._last_heading_time = None
        self._lateral_g = 0.0
        self._engage_since   = None
        self._prestart_since = None
        self._run_samples = []
        self._saved_results = None
        self._saved_range_results = None
        self._replay_sample_idx = 0
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
        """Compute _raw_g_dev for start-detection. Canvas is driven by update_payload only."""
        mag = math.sqrt(x_g ** 2 + y_g ** 2 + z_g ** 2)
        self._raw_g_dev = abs(mag - 1.0)

    def _on_gforce_trigger_toggled(self, btn: Gtk.CheckButton) -> None:
        self._gforce_trigger = btn.get_active()

    def _on_threshold_minus(self, *_: Any) -> None:
        self._engage_threshold = max(0.05, round(self._engage_threshold - 0.05, 2))
        self._threshold_label.set_text(f"{self._engage_threshold:.2f} g")
        if self.on_engage_threshold_changed:
            self.on_engage_threshold_changed(self._engage_threshold)

    def _on_threshold_plus(self, *_: Any) -> None:
        self._engage_threshold = min(1.50, round(self._engage_threshold + 0.05, 2))
        self._threshold_label.set_text(f"{self._engage_threshold:.2f} g")
        if self.on_engage_threshold_changed:
            self.on_engage_threshold_changed(self._engage_threshold)

    def set_engage_threshold(self, value: float) -> None:
        self._engage_threshold = max(0.05, min(1.50, round(float(value), 2)))
        self._threshold_label.set_text(f"{self._engage_threshold:.2f} g")
