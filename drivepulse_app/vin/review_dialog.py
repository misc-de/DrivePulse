"""Dialog: decoded VIN fields per source — user picks per-field value."""
from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from drivepulse_app.common import _translate

_FIELD_ORDER = [
    "manufacturer",
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
    """Shows decoded VIN fields grouped by source.

    * Fields where all sources agree → single checkbox row.
    * Fields where sources differ   → expander with one radio per source value.
    * User can uncheck any field to exclude it from the saved result.
    """

    def __init__(
        self,
        vin: str,
        sources: dict[str, dict[str, Any]],
        language: str = "de",
    ) -> None:
        super().__init__()
        self._language = language
        # _selections[field] = (include_check, single_value_or_None, radios)
        # single_value: str for agree-rows, None for multi-radio rows
        # radios: list of (value, radio_button) for disagree-rows
        self._selections: dict[str, tuple[Gtk.CheckButton, str | None, list[tuple[str, Gtk.CheckButton]]]] = {}

        self.set_heading(_translate(language, "cars.vin_review.title"))
        vin_display = f"…{vin[-8:]}" if len(vin) > 8 else vin
        self.set_body(_translate(language, "cars.vin_review.body", vin=vin_display))

        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        list_box.add_css_class("boxed-list")

        if sources:
            src_row = Adw.ActionRow(
                title=_translate(language, "cars.vin_review.source"),
                subtitle=" · ".join(sources.keys()),
            )
            src_row.set_activatable(False)
            list_box.append(src_row)

        has_fields = False

        for field in _FIELD_ORDER:
            source_values: dict[str, str] = {}
            for src_name, src_data in sources.items():
                v = src_data.get(field)
                if v:
                    source_values[src_name] = str(v)
            if not source_values:
                continue

            has_fields = True
            lang_suffix = _FIELD_LANG_SUFFIX.get(field, field.upper())
            label = _translate(language, f"cars.pid.{lang_suffix}")

            unique_values: list[str] = list(dict.fromkeys(source_values.values()))

            include_check = Gtk.CheckButton()
            include_check.set_active(True)
            include_check.set_valign(Gtk.Align.CENTER)

            radios: list[tuple[str, Gtk.CheckButton]] = []
            single_value: str | None = None

            if len(unique_values) == 1:
                single_value = unique_values[0]
                src_names = " · ".join(source_values.keys())
                row = Adw.ActionRow(title=label)
                row.set_subtitle(f"{unique_values[0]}  〈{src_names}〉")
                row.add_suffix(include_check)
                row.set_activatable_widget(include_check)
                list_box.append(row)

            else:
                expander = Adw.ExpanderRow(title=label)
                expander.set_expanded(True)

                first_radio: Gtk.CheckButton | None = None
                for src_name, val in source_values.items():
                    radio = Gtk.CheckButton(label=f"{val}  〈{src_name}〉")
                    radio.set_valign(Gtk.Align.CENTER)
                    if first_radio is None:
                        first_radio = radio
                        radio.set_active(True)
                    else:
                        radio.set_group(first_radio)

                    sub = Adw.ActionRow()
                    sub.add_prefix(radio)
                    sub.set_activatable_widget(radio)
                    expander.add_row(sub)
                    radios.append((val, radio))

                expander.add_suffix(include_check)
                list_box.append(expander)

            self._selections[field] = (include_check, single_value, radios)

        if not has_fields:
            empty_lbl = Gtk.Label(
                label=_translate(language, "cars.vin_review.no_data"),
                xalign=0.5,
            )
            empty_lbl.add_css_class("dim-label")
            empty_lbl.set_margin_top(12)
            empty_lbl.set_margin_bottom(12)
            list_box.append(empty_lbl)

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
        scroll.set_max_content_height(380)
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
        for include_check, _single, _radios in self._selections.values():
            include_check.set_active(active)

    def get_accepted_data(self) -> dict[str, Any]:
        """Return the user-selected value per field (only included fields)."""
        result: dict[str, Any] = {}
        for field, (include_check, single_value, radios) in self._selections.items():
            if not include_check.get_active():
                continue
            if single_value is not None:
                result[field] = single_value
            else:
                for val, radio in radios:
                    if radio.get_active():
                        result[field] = val
                        break
        return result
