"""Reusable single-child container that uniformly scales its child.

Unlike font-only DPI scaling, this scales the *entire* rendered subtree —
icons, paddings, images and text alike — with real reflow: the child is
allocated ``size / scale`` pixels and then rendered through a Gsk scale
transform, so at scale 0.5 the child lays out as if it had twice the room
and is drawn at half size (≈ double the content per axis).
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
from gi.repository import Gsk, Gtk


class ScaledContainer(Gtk.Widget):
    """Single-child container that renders its child at a uniform scale factor.

    The child is measured and allocated at ``1 / scale`` of this widget's size
    and drawn through a matching scale transform. GTK applies the allocation
    transform to input picking too, so pointer events map correctly — the same
    mechanism RotatedContainer relies on.
    """

    __gtype_name__ = "DPScaledContainer"

    def __init__(self) -> None:
        super().__init__()
        self._child: Gtk.Widget | None = None
        self._scale: float = 1.0

    def set_child(self, child: Gtk.Widget | None) -> None:
        if self._child is not None:
            self._child.unparent()
        self._child = child
        if child is not None:
            child.set_parent(self)

    def set_scale(self, scale: float) -> None:
        s = max(0.1, min(1.0, float(scale)))
        if abs(s - self._scale) < 1e-6:
            return
        self._scale = s
        self.queue_resize()

    def do_measure(self, orientation: Gtk.Orientation, for_size: int):
        if self._child is None:
            return (0, 0, -1, -1)
        s = self._scale
        child_for = round(for_size / s) if for_size >= 0 else for_size
        minimum, natural, _min_b, _nat_b = self._child.measure(orientation, child_for)
        return (round(minimum * s), round(natural * s), -1, -1)

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        if self._child is None:
            return
        s = self._scale
        if s == 1.0:
            self._child.allocate(width, height, baseline, None)
            return
        tr = Gsk.Transform.new().scale(s, s)
        self._child.allocate(round(width / s), round(height / s), -1, tr)

    def do_dispose(self) -> None:
        if self._child is not None:
            self._child.unparent()
            self._child = None
