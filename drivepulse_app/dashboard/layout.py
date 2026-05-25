"""Responsive dashboard gauge layout helpers."""
from __future__ import annotations

from typing import Any

from gi.repository import Gtk


class DashboardLayoutMixin:

    # Last (width, height, form_factor) tuple _on_size_changed applied a layout
    # for. The tick callback fires every frame; skip the relayout work when
    # nothing changed since last frame. Without this guard, 60 Hz × full gauge
    # layout (set_size_request + cars_page.set_narrow + stopwatch._apply_layout
    # + landscape/portrait switch) burns CPU continuously even on a static
    # dash. Form-factor is in the key because _apply_gauge_sizes branches on it
    # (desktop side cap), so a mobile↔desktop transition without size change
    # still needs a relayout.
    _last_layout_key: tuple[int, int, str] = (-1, -1, "")

    def _layout_tick(self, *_args: Any) -> bool:
        self._on_size_changed()
        return True

    def _on_size_changed(self, *_args: Any) -> bool:
        width = self.dashboard_page.get_width() or self.view_stack.get_width() or self.get_width()
        height = self.dashboard_page.get_height() or self.view_stack.get_height() or self.get_height()
        if width <= 0 or height <= 0:
            return False
        key = (width, height, getattr(self, "form_factor", ""))
        if key == self._last_layout_key:
            return False
        self._last_layout_key = key

        if hasattr(self, "cars_page"):
            # Phones always render the cars sidebar narrow regardless of
            # orientation; on larger displays still use the dimension-based
            # threshold so split windows / docked tablets stay responsive.
            narrow = (
                getattr(self, "form_factor", "desktop") == "mobile"
                or min(width, height) < self.CARS_NARROW_BREAKPOINT
            )
            self.cars_page.set_narrow(narrow)

        if hasattr(self, "stopwatch_page"):
            self.stopwatch_page._apply_layout(width, height)

        gauge_box_visible = self.gauge_box.get_visible()
        if gauge_box_visible is not False:
            if width >= height:
                self._set_landscape_layout(width, height)
            else:
                self._set_portrait_layout(width, height)

        return False

    # Speed gauge is this factor larger than the two side gauges.
    # side + speed + side  =  side*(2 + _SPEED_SCALE) in the primary axis.
    _SPEED_SCALE = 1.45

    # On desktop the gauges otherwise scale to fill the entire window,
    # producing an over-blown dashboard. Cap the side gauge to a tasteful
    # size; the speed gauge follows via _SPEED_SCALE.
    _DESKTOP_SIDE_CAP = 280

    def _set_landscape_layout(self, width: int, height: int) -> None:
        self.gauge_box.set_orientation(Gtk.Orientation.HORIZONTAL)
        self.gauge_box.set_spacing(16)
        self.gauge_box.set_halign(Gtk.Align.CENTER)
        self.gauge_box.set_valign(Gtk.Align.CENTER)

        footer_height = max(0, self.footer.get_height()) if self.footer.get_visible() else 0
        avail_w = max(1, width - 24)
        avail_h = max(1, height - 24 - footer_height - 8)

        # Solve: side*(2 + scale) + 2*spacing = avail_w  AND  speed = side*scale ≤ avail_h
        side = int(min(
            (avail_w - 32) / (2 + self._SPEED_SCALE),
            avail_h / self._SPEED_SCALE,
        ))
        side = max(1, side)
        speed = max(1, min(int(side * self._SPEED_SCALE), avail_h))

        self._apply_gauge_sizes(side, speed)

    def _set_portrait_layout(self, width: int, height: int) -> None:
        self.gauge_box.set_orientation(Gtk.Orientation.VERTICAL)
        self.gauge_box.set_spacing(8)
        self.gauge_box.set_halign(Gtk.Align.CENTER)
        self.gauge_box.set_valign(Gtk.Align.CENTER)

        footer_height = max(0, self.footer.get_height()) if self.footer.get_visible() else 0
        avail_w = max(1, width - 24)
        avail_h = max(1, height - 24 - footer_height - 8)

        # Solve: side*(2 + scale) + 2*spacing = avail_h  AND  speed = side*scale ≤ avail_w
        side = int(min(
            (avail_h - 16) / (2 + self._SPEED_SCALE),
            avail_w / self._SPEED_SCALE,
        ))
        side = max(1, side)
        speed = max(1, min(int(side * self._SPEED_SCALE), avail_w))

        self._apply_gauge_sizes(side, speed)

    def _apply_gauge_sizes(self, side: int, speed: int) -> None:
        if getattr(self, "form_factor", "mobile") == "desktop" and side > self._DESKTOP_SIDE_CAP:
            side = self._DESKTOP_SIDE_CAP
            speed = max(1, int(side * self._SPEED_SCALE))
        for gauge, sz in (
            (self.rpm_gauge,   side),
            (self.speed_gauge, speed),
            (self.temp_gauge,  side),
        ):
            gauge.set_hexpand(False)
            gauge.set_vexpand(False)
            gauge.set_halign(Gtk.Align.CENTER)
            gauge.set_valign(Gtk.Align.CENTER)
            gauge.set_size_request(sz, sz)
            gauge.set_content_width(sz)
            gauge.set_content_height(sz)

