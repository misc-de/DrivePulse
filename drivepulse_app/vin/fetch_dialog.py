"""Dialog that shows live VIN fetch progress per source."""
from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from drivepulse_app.common import _translate

_SOURCE_ORDER = ["NHTSA", "auto.dev", "vindecoder.eu"]


class VinFetchDialog(Adw.AlertDialog):
    """Per-source progress rows; 'Weiter' button appears when data is ready."""

    def __init__(
        self,
        vin: str,
        active_sources: list[str],
        language: str = "de",
    ) -> None:
        super().__init__()
        self._language = language
        self._result_sources: dict[str, Any] = {}
        self._row_widgets: dict[str, tuple[Adw.ActionRow, Gtk.Spinner]] = {}

        vin_display = f"…{vin[-8:]}" if len(vin) > 8 else vin
        self.set_heading(f"{_translate(language, 'vin.fetch.title')} · {vin_display}")

        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        list_box.add_css_class("boxed-list")
        list_box.set_margin_top(8)
        list_box.set_margin_bottom(4)

        for src in _SOURCE_ORDER:
            if src not in active_sources:
                continue
            spinner = Gtk.Spinner()
            spinner.set_spinning(True)
            spinner.set_size_request(16, 16)
            row = Adw.ActionRow(title=src)
            row.set_subtitle(_translate(language, "vin.fetch.source.pending"))
            row.add_suffix(spinner)
            list_box.append(row)
            self._row_widgets[src] = (row, spinner)

        if not self._row_widgets:
            lbl = Gtk.Label(label=_translate(language, "vin.fetch.no_source"))
            lbl.set_margin_top(8)
            lbl.set_margin_bottom(4)
            self.set_extra_child(lbl)
        else:
            self.set_extra_child(list_box)

        self.add_response("close", _translate(language, "vin.fetch.close"))
        self.set_close_response("close")

    def set_source_result(
        self,
        source: str,
        ok: bool,
        msg: str,
        field_count: int,
    ) -> None:
        """Update a source row. Must be called on the GTK main thread."""
        if source not in self._row_widgets:
            return
        row, spinner = self._row_widgets[source]
        spinner.set_spinning(False)
        spinner.set_visible(False)

        icon = Gtk.Image()
        if ok and field_count > 0:
            icon.set_from_icon_name("emblem-ok-symbolic")
            icon.add_css_class("success")
            row.set_subtitle(
                _translate(self._language, "vin.fetch.source.ok", count=str(field_count))
            )
        elif ok:
            icon.set_from_icon_name("dialog-warning-symbolic")
            icon.add_css_class("warning")
            row.set_subtitle(_translate(self._language, "vin.fetch.source.no_data"))
        else:
            icon.set_from_icon_name("dialog-error-symbolic")
            icon.add_css_class("error")
            row.set_subtitle(msg or _translate(self._language, "vin.fetch.source.no_data"))

        row.add_suffix(icon)

    def set_all_done(self, sources: dict[str, Any]) -> None:
        """Mark all fetches complete. Must be called on the GTK main thread."""
        self._result_sources = sources
        if sources:
            self.add_response("proceed", _translate(self._language, "vin.fetch.proceed"))
            self.set_default_response("proceed")
        else:
            self.set_body(_translate(self._language, "vin.fetch.no_results"))

    def get_result_sources(self) -> dict[str, Any]:
        return self._result_sources
