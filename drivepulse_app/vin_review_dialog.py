"""Dialog: decoded VIN fields, user selects which to keep."""
from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from .common import _translate
from .vin_api import SOURCE_KEY_AUTODEV, SOURCE_KEY_NHTSA, SOURCE_KEY_VINDECODER

_FIELD_ORDER = [
    "manufacturer",
    "make",
    "model",
    "year",
    "body",
    "fuel",
    "drive",
    "cylinders",
    "displacement",
    "transmission",
    "plant_country",
]

_FIELD_LANG_SUFFIX: dict[str, str] = {
    "manufacturer":  "VIN_MANUFACTURER",
    "make":          "VIN_MAKE",
    "model":         "VIN_MODEL",
    "year":          "VIN_YEAR",
    "body":          "VIN_BODY",
    "fuel":          "VIN_FUEL",
    "drive":         "VIN_DRIVE",
    "cylinders":     "VIN_CYLINDERS",
    "displacement":  "VIN_DISPLACEMENT",
    "transmission":  "VIN_TRANSMISSION",
    "plant_country": "VIN_COUNTRY",
}


class VinReviewDialog(Adw.AlertDialog):
    """Shows fetched VIN fields — user checks which ones to accept."""

    def __init__(self, vin: str, data: dict[str, Any], language: str = "de") -> None:
        super().__init__()
        self._language = language
        self._checks: list[tuple[str, str, Gtk.CheckButton]] = []

        self.set_heading(_translate(language, "cars.vin_review.title"))
        self.set_body(_translate(language, "cars.vin_review.body", vin=f"…{vin[-8:]}" if len(vin) > 8 else vin))

        sources: list[str] = []
        if data.get(SOURCE_KEY_NHTSA):
            sources.append("NHTSA")
        if data.get(SOURCE_KEY_AUTODEV):
            sources.append("auto.dev")
        if data.get(SOURCE_KEY_VINDECODER):
            sources.append("vindecoder.eu")

        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        list_box.add_css_class("boxed-list")

        if sources:
            src_row = Adw.ActionRow(
                title=_translate(language, "cars.vin_review.source"),
                subtitle=" · ".join(sources),
            )
            src_row.set_activatable(False)
            list_box.append(src_row)

        has_fields = False
        for field in _FIELD_ORDER:
            value = data.get(field)
            if not value:
                continue
            has_fields = True
            lang_suffix = _FIELD_LANG_SUFFIX.get(field, field.upper())
            label = _translate(language, f"cars.pid.{lang_suffix}")

            check = Gtk.CheckButton()
            check.set_active(True)
            check.set_valign(Gtk.Align.CENTER)

            row = Adw.ActionRow(title=label, subtitle=str(value))
            row.add_suffix(check)
            row.set_activatable_widget(check)
            list_box.append(row)
            self._checks.append((field, str(value), check))

        if not has_fields:
            empty_lbl = Gtk.Label(
                label=_translate(language, "cars.vin_review.no_data"),
                xalign=0.5,
            )
            empty_lbl.add_css_class("dim-label")
            empty_lbl.set_margin_top(12)
            empty_lbl.set_margin_bottom(12)
            list_box.append(empty_lbl)

        # Select-all / deselect-all toolbar
        sel_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sel_box.set_margin_top(6)
        sel_box.set_margin_start(2)
        sel_box.set_margin_end(2)

        all_btn = Gtk.Button(label=_translate(language, "cars.vin_review.select_all"))
        all_btn.add_css_class("flat")
        all_btn.add_css_class("caption")
        all_btn.connect("clicked", lambda _b: self._set_all(True))

        none_btn = Gtk.Button(label=_translate(language, "cars.vin_review.select_none"))
        none_btn.add_css_class("flat")
        none_btn.add_css_class("caption")
        none_btn.connect("clicked", lambda _b: self._set_all(False))

        sel_box.append(all_btn)
        sel_box.append(none_btn)

        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        wrap.append(sel_box)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_max_content_height(340)
        scroll.set_propagate_natural_height(True)
        scroll.set_child(list_box)
        wrap.append(scroll)

        self.set_extra_child(wrap)

        self.add_response("cancel", _translate(language, "cars.vin_review.cancel"))
        self.add_response("accept", _translate(language, "cars.vin_review.accept"))
        self.set_response_appearance("accept", Adw.ResponseAppearance.SUGGESTED)
        self.set_default_response("accept")
        self.set_close_response("cancel")

    def _set_all(self, active: bool) -> None:
        for _field, _val, check in self._checks:
            check.set_active(active)

    def get_accepted_data(self) -> dict[str, Any]:
        """Return only the fields the user left checked."""
        return {field: value for field, value, check in self._checks if check.get_active()}
