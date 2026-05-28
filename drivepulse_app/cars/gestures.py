"""Horizontal-swipe gestures on CarsPage.

The list root reacts to a horizontal swipe as a tab switch (back / forward via
``on_back_swipe`` / ``on_forward_swipe``). Inside the detail navigation a
rightward swipe always pops one level — the user expects "swipe right = back",
independent of whether the Adw.NavigationView swipe would have triggered.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from gi.repository import Adw, Gtk


class CarsGesturesMixin:
    # Concrete CarsPage state surfaced to this mixin. See
    # project_mixin_typing.md.
    _drag_claimed: bool
    _detail_pushed: bool
    _detail_page: Any
    nav_view: Adw.NavigationView
    on_back_swipe: Callable[[], None] | None
    on_forward_swipe: Callable[[], None] | None
    _on_detail_back: Callable[..., Any]

    def _on_drag_begin(self, _gesture: Gtk.GestureDrag, _x: float, _y: float) -> None:
        self._drag_claimed = False

    def _on_drag_update(self, gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        if self._drag_claimed:
            return
        dist_sq = offset_x * offset_x + offset_y * offset_y
        if dist_sq < 64:  # less than 8 px — direction still unclear
            return
        # Clearly vertical → deny so ScrolledWindow children can scroll.
        if abs(offset_y) > abs(offset_x) * 1.5:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        # Detail open + swipe right: pop the detail view ourselves so the user
        # gets a consistent "swipe right = back to list" instead of relying on
        # the Adw.NavigationView swipe (which often does not trigger here).
        if self._detail_pushed and abs(offset_x) > 20 and offset_x > 0:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self._drag_claimed = True
            return
        if self._detail_pushed:
            return
        # Clearly horizontal (at least 20 px, X clearly dominant).
        if abs(offset_x) > 20:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self._drag_claimed = True

    def _on_drag_end(self, _gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        if not self._drag_claimed:
            return
        self._drag_claimed = False
        # In the detail view: swipe right = back to the car list, instead of
        # jumping to the previous tab like on the root.
        if self._detail_pushed:
            if offset_x > 60:
                # If a sub-page (chart, trip, scan…) sits on top of the detail
                # root, the swipe only pops one level → back to the car detail
                # page instead of breaking all the way back to the list.
                visible = self.nav_view.get_visible_page()
                if visible is not None and visible is not self._detail_page:
                    self.nav_view.pop()
                else:
                    self._on_detail_back()
            return
        if offset_x > 60 and self.on_back_swipe is not None:
            self.on_back_swipe()
        elif offset_x < -60 and self.on_forward_swipe is not None:
            self.on_forward_swipe()
