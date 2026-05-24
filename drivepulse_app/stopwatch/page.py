"""StopWatch (0-100/0-200 timing) page for DrivePulse."""
from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango

from drivepulse_app.common import SOURCE_LANGUAGE, _make_label_responsive, _normalize_language, _translate
from drivepulse_app.stopwatch.canvas import GForceCanvas
from drivepulse_app.stopwatch.processing import StopWatchProcessingMixin
from drivepulse_app.stopwatch.replay import StopWatchReplayMixin

_WARNING_CSS = (
    b"button.warning-reset{background:rgba(229,165,10,0.85);color:#1c1c1c;}"
    b"button.warning-reset:hover{background:rgba(200,144,8,0.9);}"
    b".dp-accel-light .card{background:rgba(255,255,255,0.82);color:#000000;}"
    b".dp-accel-light label{color:#000000;}"
    b".dp-accel-light .dim-label{color:rgba(0,0,0,0.62);}"
    b".dp-accel-light checkbutton{color:#000000;}"
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


class StopWatchPage(StopWatchProcessingMixin, StopWatchReplayMixin, Gtk.Box):
    __gtype_name__ = "StopWatchPage"

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
        # Returns True on successful persistence, False if it could not save
        # (e.g. no active vehicle). Legacy None-returning callbacks are treated
        # as success for backward compatibility.
        self.on_run_complete: Callable[[dict, list], bool | None] | None = None
        self._run_samples: list[tuple[float, float | None, float]] = []  # (elapsed, active_g, lateral_g)
        self._saved_results: dict | None = None
        self._saved_range_results: dict | None = None
        self._run_persisted: bool = False  # True after the current run was written to the DB
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

        self.save_button = Gtk.Button()
        self.save_button.add_css_class("suggested-action")
        self.save_button.add_css_class("pill")
        self.save_button.set_hexpand(True)
        self.save_button.set_visible(False)
        self.save_button.connect("clicked", self._on_save_clicked)

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

        self._threshold_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._threshold_controls.set_halign(Gtk.Align.CENTER)
        self._threshold_controls.append(self._threshold_minus)
        self._threshold_controls.append(self._threshold_label)
        self._threshold_controls.append(self._threshold_plus)

        self._trigger_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._trigger_row.set_halign(Gtk.Align.FILL)
        self.gforce_trigger_check.set_halign(Gtk.Align.CENTER)
        self._trigger_row.append(self.gforce_trigger_check)
        self._trigger_row.append(self._threshold_controls)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.set_margin_top(8)
        controls.set_margin_bottom(8)
        controls.set_hexpand(True)
        controls.append(self.start_button)
        controls.append(self.abort_button)
        controls.append(self.replay_button)
        controls.append(self.reset_button)

        save_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        save_row.set_margin_bottom(20)
        save_row.set_hexpand(True)
        save_row.append(self.save_button)
        self._save_row = save_row

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

        # Results table: always render at natural height so every row stays
        # visible; other widgets adapt to whatever space is left.
        self.results_box.set_hexpand(True)
        self.results_box.set_vexpand(False)
        self.results_box.set_valign(Gtk.Align.START)
        self.results_scroll = Gtk.ScrolledWindow()
        self.results_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
        self.results_scroll.set_propagate_natural_height(True)
        self.results_scroll.set_hexpand(True)
        self.results_scroll.set_vexpand(False)
        self.results_scroll.set_valign(Gtk.Align.START)
        self.results_scroll.set_child(self.results_box)

        # Wrap trigger + controls into a box that gets reparented by _apply_layout:
        # inside left_col in landscape, directly on StopWatchPage in portrait.
        self._trigger_row.set_margin_top(8)
        self._bottom_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._bottom_box.set_hexpand(True)
        self._bottom_box.append(self._trigger_row)
        self._bottom_box.append(controls)
        self._bottom_box.append(save_row)

        # left_col: intro + table only; _bottom_box is added by _apply_layout.
        self.left_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.left_col.set_hexpand(True)
        self.left_col.set_vexpand(False)
        self.left_col.set_valign(Gtk.Align.START)
        self.left_col.append(intro)
        self.left_col.append(self.results_scroll)

        # content_box switches between HORIZONTAL (landscape) and VERTICAL (portrait).
        self.content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.content_box.set_hexpand(True)
        self.content_box.set_vexpand(True)
        self.content_box.append(self.left_col)
        self.content_box.append(self.gforce_box)

        # Portrait initial placement: content above, options at very bottom.
        self.append(self.content_box)
        self.append(self._bottom_box)

        self._current_layout = "portrait"

        self._refresh_texts()

    # ------------------------------------------------------------------
    # Responsive layout — 50/50 split in landscape, stacked in portrait
    # ------------------------------------------------------------------

    def _layout_target_for_size(self, width: int, height: int) -> str:
        return "landscape" if width >= height else "portrait"

    def _apply_layout(self, width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            return
        target = self._layout_target_for_size(width, height)
        if target == self._current_layout:
            return
        self._current_layout = target
        if target == "landscape":
            self.content_box.set_orientation(Gtk.Orientation.HORIZONTAL)
            self.content_box.set_spacing(12)
            self.content_box.set_homogeneous(True)
            # left_col fills its half vertically so gforce_box aligns with the title
            self.left_col.set_vexpand(True)
            self.left_col.set_valign(Gtk.Align.FILL)
            # results_scroll always shows full table; remaining space goes to gforce/options
            self.results_scroll.set_vexpand(False)
            self.results_scroll.set_valign(Gtk.Align.START)
            self.results_scroll.set_propagate_natural_height(True)
            self.maxes_label.set_margin_top(8)
            # Trigger row: checkbox and ± on one line
            self._trigger_row.set_orientation(Gtk.Orientation.HORIZONTAL)
            self._trigger_row.set_spacing(8)
            self.gforce_trigger_check.set_halign(Gtk.Align.START)
            self.gforce_trigger_check.set_hexpand(True)
            self._threshold_controls.set_halign(Gtk.Align.END)
            # Move options into left_col (below results table)
            self.remove(self._bottom_box)
            self.left_col.append(self._bottom_box)
        else:
            self.content_box.set_orientation(Gtk.Orientation.VERTICAL)
            self.content_box.set_spacing(0)
            self.content_box.set_homogeneous(False)
            # left_col takes natural height; gforce_box expands below
            self.left_col.set_vexpand(False)
            self.left_col.set_valign(Gtk.Align.START)
            # table sits compactly, canvas expands below
            self.results_scroll.set_vexpand(False)
            self.results_scroll.set_valign(Gtk.Align.START)
            self.results_scroll.set_propagate_natural_height(True)
            self.maxes_label.set_margin_top(24)
            # Trigger row: checkbox and ± stacked
            self._trigger_row.set_orientation(Gtk.Orientation.VERTICAL)
            self._trigger_row.set_spacing(4)
            self.gforce_trigger_check.set_halign(Gtk.Align.CENTER)
            self.gforce_trigger_check.set_hexpand(False)
            self._threshold_controls.set_halign(Gtk.Align.CENTER)
            # Move options to very bottom of the page (below gforce canvas)
            self.left_col.remove(self._bottom_box)
            self.append(self._bottom_box)

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        # Apply layout BEFORE the parent allocates children so they are
        # positioned with the correct orientation in the same frame.
        self._apply_layout(width, height)
        Gtk.Box.do_size_allocate(self, width, height, baseline)

    # ------------------------------------------------------------------
    # Layout helpers
    # ------------------------------------------------------------------

    def _build_result_rows(self) -> None:
        self._col_obd_sg = self._make_size_group()
        self._col_gps_sg = self._make_size_group()
        self._col_best_sg = self._make_size_group()

        first = True
        for target in self.SPEED_TARGETS_KMH:
            self.results_box.append(
                self._make_result_row(f"0–{target} km/h", target, show_captions=first)
            )
            first = False
        for lo, hi in self.RANGE_TARGETS_KMH:
            self.results_box.append(
                self._make_result_row(f"{lo}–{hi} km/h", (lo, hi), show_captions=False)
            )
        self.results_box.append(self._make_result_row("Vmax", "vmax", show_captions=False))

    def _make_size_group(self) -> Any:
        size_group = getattr(Gtk, "SizeGroup", None)
        size_group_mode = getattr(getattr(Gtk, "SizeGroupMode", None), "HORIZONTAL", None)
        if size_group is None or size_group_mode is None:
            return _NoopSizeGroup()
        return size_group(mode=size_group_mode)

    def _make_result_row(self, label_text: str, key: Any, *, show_captions: bool = True) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.add_css_class("card")
        row.add_css_class("dp-table-row")
        row.set_margin_top(2)
        row.set_margin_bottom(2)

        name_lbl = Gtk.Label(label=label_text)
        name_lbl.add_css_class("heading")
        name_lbl.set_halign(Gtk.Align.START)
        name_lbl.set_valign(Gtk.Align.END)
        name_lbl.set_hexpand(True)
        name_lbl.set_margin_start(10)
        ellipsize_mode = getattr(getattr(Pango, "EllipsizeMode", None), "END", None)
        if ellipsize_mode is not None:
            name_lbl.set_ellipsize(ellipsize_mode)
        name_lbl.set_max_width_chars(10)

        def _make_col(caption: str, sg: Any, initially_hidden: bool) -> tuple[Gtk.Box, Gtk.Label, Gtk.Label]:
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            col.set_halign(Gtk.Align.END)
            col.set_valign(Gtk.Align.CENTER)
            col.set_margin_end(6)
            cap_lbl = Gtk.Label(label=caption)
            cap_lbl.add_css_class("caption")
            cap_lbl.add_css_class("dim-label")
            cap_lbl.set_xalign(1.0)
            if not show_captions:
                # Keep the caption slot in the layout so the value below stays
                # bottom-aligned to row 1's value; just render the caption text invisible.
                cap_lbl.set_opacity(0.0)
            val_lbl = Gtk.Label(label="--")
            val_lbl.add_css_class("monospace")
            val_lbl.set_xalign(1.0)
            sg.add_widget(val_lbl)
            col.append(cap_lbl)
            col.append(val_lbl)
            if initially_hidden:
                col.set_visible(False)
            return col, cap_lbl, val_lbl

        obd_col, obd_cap, obd_val = _make_col("OBD", self._col_obd_sg, initially_hidden=True)
        gps_col, gps_cap, gps_val = _make_col("GPS", self._col_gps_sg, initially_hidden=True)
        best_col, best_cap, best_val = _make_col(
            _translate(self.language, "stopwatch.best"), self._col_best_sg, initially_hidden=False
        )
        best_col.set_margin_end(10)
        best_val.add_css_class("heading")

        row.append(name_lbl)
        row.append(obd_col)
        row.append(gps_col)
        row.append(best_col)

        self.result_labels[(key, "obd")] = obd_val
        self.result_labels[(key, "gps")] = gps_val
        self.result_labels[(key, "best")] = best_val
        self._obd_captions[key] = obd_cap
        self._gps_captions[key] = gps_cap
        self._best_captions[key] = best_cap
        # source_rows now points to the whole column box so set_visible() hides caption + value together
        self.source_rows[(key, "obd")] = obd_col
        self.source_rows[(key, "gps")] = gps_col
        if key == "vmax":
            self._vmax_name_lbl = name_lbl
        return row

    def _all_keys(self) -> list[Any]:
        return list(self.SPEED_TARGETS_KMH) + list(self.RANGE_TARGETS_KMH)

    # ------------------------------------------------------------------
    # Text / language
    # ------------------------------------------------------------------

    def _refresh_texts(self) -> None:
        self.title_label.set_text(_translate(self.language, "stopwatch.title"))
        self.start_button.set_label(_translate(self.language, "stopwatch.start"))
        self.abort_button.set_label(_translate(self.language, "stopwatch.abort"))
        self.reset_button.set_label(_translate(self.language, "stopwatch.reset"))
        self.save_button.set_label(_translate(self.language, "stopwatch.save"))
        self.gforce_trigger_check.set_label(_translate(self.language, "stopwatch.gforce_trigger"))
        if self.replay_button.get_visible() and not self._replay_active:
            self.replay_button.set_label(_translate(self.language, "stopwatch.replay"))
        if not self.armed and not self.running:
            self.status_label.set_text(_translate(self.language, "stopwatch.ready"))
        best_text = _translate(self.language, "stopwatch.best")
        for lbl in self._best_captions.values():
            lbl.set_text(best_text)
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

    def set_theme_mode(self, mode: str) -> None:
        light = mode == "light"
        if light:
            self.add_css_class("dp-accel-light")
        else:
            self.remove_css_class("dp-accel-light")
        self.gforce_canvas.set_light_mode(light)

    def _set_g_text(self, active_g: float | None) -> None:
        if active_g is None:
            self.g_label.set_text(_translate(self.language, "stopwatch.g.empty"))
        else:
            self.g_label.set_text(_translate(self.language, "stopwatch.g", value=f"{active_g:.3f}"))

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
            obd_box.set_visible(self._obd_ever_seen or obd_v is not None)
        if gps_box:
            gps_box.set_visible(self._gps_ever_seen or gps_v is not None)

        if self._vmax_name_lbl is not None:
            self._vmax_name_lbl.set_text("Vmax")

        if obd_lbl:
            obd_lbl.set_text(f"{obd_t:.2f} s" if obd_t is not None else "--")
        if gps_lbl:
            gps_lbl.set_text(f"{gps_t:.2f} s" if gps_t is not None else "--")

        if best_lbl:
            times = [t for t in [obd_t, gps_t] if t is not None]
            best_lbl.set_text(f"{sum(times) / len(times):.2f} s" if times else "--")

    def _update_maxes_label(self) -> None:
        vmax = self.max_obd_speed if self.max_obd_speed is not None else self.max_gps_speed
        parts: list[str] = []
        if vmax is not None:
            parts.append(_translate(self.language, "stopwatch.vmax", value=f"{vmax:.0f} km/h"))
        else:
            parts.append(_translate(self.language, "stopwatch.vmax.empty"))
        if self.max_g is not None:
            parts.append(_translate(self.language, "stopwatch.gmax", value=f"{self.max_g:.1f}"))
        else:
            parts.append(_translate(self.language, "stopwatch.gmax.empty"))
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
        self.replay_button.set_label(_translate(self.language, "stopwatch.replay"))
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
        self._run_persisted = False
        self.save_button.set_visible(False)
        self._reset_labels()
        self._set_source_visibility(False, False)
        self._update_maxes_label()
        self._show_abort()
        self.status_label.set_text(_translate(self.language, "stopwatch.armed"))
        if self.on_mock_start is not None:
            self.on_mock_start()

    def abort_measurement(self, *_args: Any) -> None:
        self.armed = False
        self.running = False
        self._show_start()
        self.status_label.set_text(_translate(self.language, "stopwatch.done"))
        if self._has_unsaved_data():
            self.save_button.set_visible(True)

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
        self._run_persisted = False
        self.save_button.set_visible(False)
        self._replay_sample_idx = 0
        self.gforce_canvas.clear()
        self.results = {target: {"obd": None, "gps": None} for target in self.SPEED_TARGETS_KMH}
        self.range_results = {r: {"obd": None, "gps": None} for r in self.RANGE_TARGETS_KMH}
        self._reset_labels()
        self._set_source_visibility(False, False)
        self._update_maxes_label()
        self._show_start()
        self._set_g_text(None)
        self.status_label.set_text(_translate(self.language, "stopwatch.ready"))

    def _has_unsaved_data(self) -> bool:
        """True if the current measurement state contains anything worth persisting."""
        if self._run_persisted:
            return False
        if self._run_samples:
            return True
        if self.max_g is not None or self.max_obd_speed is not None or self.max_gps_speed is not None:
            return True
        for row in self.results.values():
            if row["obd"] is not None or row["gps"] is not None:
                return True
        return any(
            row["obd"] is not None or row["gps"] is not None
            for row in self.range_results.values()
        )

    def load_persisted_run(self, data: dict[str, Any]) -> bool:
        """Restore a saved run into the page so it can be replayed.

        Accepts the dict shape returned by ``DriveDB.get_stopwatch_run``
        (``{"results": {...}, "samples": [...]}``). Sample lists may be either
        the canonical ``[elapsed, active_g, lateral_g]`` triplets persisted by
        the stopwatch itself, or richer dicts (the mock seeder uses
        ``{"ts", "speed_obd_kmh", "speed_gps_kmh", "accel_g", "rpm"}``).

        Returns True on success.
        """
        results_blob = (data or {}).get("results") or {}
        samples_blob = (data or {}).get("samples") or []

        # Stop any ongoing measurement / replay before we overwrite state.
        self._stop_replay()
        self.armed = False
        self.running = False
        self.start_monotonic = None

        # ── Targets ────────────────────────────────────────────────────────
        new_results: dict[int, dict[str, float | None]] = {
            target: {"obd": None, "gps": None} for target in self.SPEED_TARGETS_KMH
        }
        for key, val in (results_blob.get("targets") or {}).items():
            try:
                tgt = int(str(key))
            except (TypeError, ValueError):
                continue
            if tgt not in new_results or not isinstance(val, dict):
                continue
            new_results[tgt] = {
                "obd": val.get("obd") if isinstance(val.get("obd"), (int, float)) else None,
                "gps": val.get("gps") if isinstance(val.get("gps"), (int, float)) else None,
            }
        self.results = new_results
        self._saved_results = {k: dict(v) for k, v in new_results.items()}

        # ── Ranges (accept both "(100, 200)" tuple-repr and "100-200" forms)
        new_ranges: dict[tuple[int, int], dict[str, float | None]] = {
            r: {"obd": None, "gps": None} for r in self.RANGE_TARGETS_KMH
        }

        def _parse_range_key(raw: str) -> tuple[int, int] | None:
            s = str(raw).strip().lstrip("(").rstrip(")")
            sep = "," if "," in s else ("-" if "-" in s else None)
            if sep is None:
                return None
            try:
                lo_s, hi_s = s.split(sep, 1)
                return int(lo_s.strip()), int(hi_s.strip())
            except (TypeError, ValueError):
                return None

        for key, val in (results_blob.get("ranges") or {}).items():
            parsed = _parse_range_key(key)
            if parsed is None or parsed not in new_ranges or not isinstance(val, dict):
                continue
            new_ranges[parsed] = {
                "obd": val.get("obd") if isinstance(val.get("obd"), (int, float)) else None,
                "gps": val.get("gps") if isinstance(val.get("gps"), (int, float)) else None,
            }
        self.range_results = new_ranges
        self._saved_range_results = {k: dict(v) for k, v in new_ranges.items()}

        # ── Vmax + max-g ─────────────────────────────────────────────────
        self._saved_vmax_obd = results_blob.get("max_obd_kmh")
        self._saved_vmax_obd_t = results_blob.get("max_obd_t")
        self._saved_vmax_gps = results_blob.get("max_gps_kmh")
        self._saved_vmax_gps_t = results_blob.get("max_gps_t")
        self.max_obd_speed = self._saved_vmax_obd
        self._max_obd_speed_t = self._saved_vmax_obd_t
        self.max_gps_speed = self._saved_vmax_gps
        self._max_gps_speed_t = self._saved_vmax_gps_t
        self.max_g = results_blob.get("max_g")

        # ── Samples ──────────────────────────────────────────────────────
        triplets: list[tuple[float, float | None, float]] = []
        for s in samples_blob:
            if isinstance(s, dict):
                ts = s.get("ts") or s.get("elapsed")
                active_g = s.get("accel_g") or s.get("active_g")
                lat_g = s.get("lateral_g", 0.0) or 0.0
                if ts is None:
                    continue
                try:
                    triplets.append((float(ts), None if active_g is None else float(active_g), float(lat_g)))
                except (TypeError, ValueError):
                    continue
            elif isinstance(s, (list, tuple)) and len(s) >= 3:
                try:
                    triplets.append((float(s[0]), None if s[1] is None else float(s[1]), float(s[2])))
                except (TypeError, ValueError):
                    continue
        self._run_samples = triplets

        # Persisted runs are immutable from the StopWatch perspective.
        self._run_persisted = True
        if hasattr(self, "save_button"):
            self.save_button.set_visible(False)

        # Repaint labels and reveal the replay button.
        self._reset_labels()
        self._set_source_visibility(True, True)
        self._update_vmax_row(
            obd_v=self._saved_vmax_obd, obd_t=self._saved_vmax_obd_t,
            gps_v=self._saved_vmax_gps, gps_t=self._saved_vmax_gps_t,
        )
        self._update_maxes_label()
        self._show_replay()
        self.status_label.set_text(_translate(self.language, "stopwatch.loaded"))
        return True

    def _build_run_payload(self) -> tuple[dict, list]:
        combined = {
            "targets": {str(k): dict(v) for k, v in self.results.items()},
            "ranges": {str(k): dict(v) for k, v in self.range_results.items()},
            "max_obd_kmh": self.max_obd_speed,
            "max_obd_t": self._max_obd_speed_t,
            "max_gps_kmh": self.max_gps_speed,
            "max_gps_t": self._max_gps_speed_t,
            "max_g": self.max_g,
        }
        samples_list = [list(s) for s in self._run_samples]
        return combined, samples_list

    def reveal_save_button(self) -> None:
        """Called by the processing mixin when a measurement finishes successfully."""
        if self._has_unsaved_data():
            self.save_button.set_visible(True)

    def _on_save_clicked(self, *_args: Any) -> None:
        if self._run_persisted or not self._has_unsaved_data():
            self.save_button.set_visible(False)
            return
        if self.on_run_complete is None:
            return
        combined, samples_list = self._build_run_payload()
        try:
            ok = self.on_run_complete(combined, samples_list)
        except Exception:
            return
        if ok is False:
            self.status_label.set_text(_translate(self.language, "stopwatch.save.no_car"))
            return
        self._run_persisted = True
        self.save_button.set_visible(False)
        self.status_label.set_text(_translate(self.language, "stopwatch.saved"))

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
