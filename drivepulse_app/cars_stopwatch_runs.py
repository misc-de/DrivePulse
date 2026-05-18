"""StopWatch-run list and detail helpers for CarsPage."""
from __future__ import annotations

from typing import Any

from gi.repository import Adw, GLib, Gtk

from .common import _translate
from .diagnostics import get_logger


log = get_logger(__name__)


class CarsStopWatchRunsMixin:
    def _render_stopwatch_runs_into_value_list(self) -> None:
        if self.db is None or self._selected_car_id is None:
            self.value_list.append(self._info_row(_translate(self.language, "cars.stopwatch_runs.empty")))
            return
        try:
            runs = self.db.list_stopwatch_runs_for_car(self._selected_car_id)
        except Exception:
            log.exception("Could not list stopwatch runs for car id=%s", self._selected_car_id)
            runs = []
        if not runs:
            self.value_list.append(self._info_row(_translate(self.language, "cars.stopwatch_runs.empty")))
            return
        for run in runs:
            self.value_list.append(self._make_stopwatch_run_row(run))

    def _make_stopwatch_run_row(self, run: Any) -> Adw.ActionRow:
        row = Adw.ActionRow()
        run_id = int(run["id"])
        ts = self._parse_ts(run["run_at"])
        title = ts.strftime("%d.%m.%Y · %H:%M") if ts else _translate(self.language, "cars.stopwatch_run.title", date=str(run_id))
        row.set_title(GLib.markup_escape_text(title))

        parts: list[str] = []
        try:
            data = self.db.get_stopwatch_run(run_id)
            results = data.get("results", {})
            targets = results.get("targets", {})
            max_obd = results.get("max_obd_kmh")
            max_gps = results.get("max_gps_kmh")
            if max_obd is not None:
                parts.append(f"OBD {max_obd:.0f} km/h")
            if max_gps is not None:
                parts.append(f"GPS {max_gps:.0f} km/h")
            count = len([v for v in targets.values() if v.get("obd") is not None or v.get("gps") is not None])
            if count:
                parts.append(f"{count} Zeiten")
        except Exception:
            log.exception("Could not load stopwatch run summary id=%s", run_id)
        row.set_subtitle(GLib.markup_escape_text(" · ".join(parts)) if parts else "")
        row.set_subtitle_lines(0)

        icon = Gtk.Image.new_from_icon_name("stopwatch-symbolic")
        row.add_prefix(icon)
        chev = Gtk.Image.new_from_icon_name("go-next-symbolic")
        row.add_suffix(chev)
        row.set_activatable(True)
        row.connect("activated", lambda _r, rid=run_id: self._open_stopwatch_run_detail(rid))
        return row

    def _open_stopwatch_run_detail(self, run_id: int) -> None:
        if self.db is None:
            return
        try:
            data = self.db.get_stopwatch_run(run_id)
        except Exception:
            log.exception("Could not open stopwatch run detail id=%s", run_id)
            return
        if not data:
            return

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_child(box)

        results = data.get("results", {})
        targets = results.get("targets", {})
        ranges = results.get("ranges", {})
        max_obd = results.get("max_obd_kmh")
        max_gps = results.get("max_gps_kmh")
        max_g = results.get("max_g")

        group = Adw.PreferencesGroup()
        group.set_margin_top(12)
        group.set_margin_bottom(12)
        group.set_margin_start(12)
        group.set_margin_end(12)
        box.append(group)

        def _add_row(title: str, val: str) -> None:
            r = Adw.ActionRow()
            r.set_title(GLib.markup_escape_text(title))
            lbl = Gtk.Label(label=val)
            lbl.add_css_class("monospace")
            lbl.set_valign(Gtk.Align.CENTER)
            r.add_suffix(lbl)
            r.set_activatable(False)
            group.add(r)

        if max_obd is not None:
            _add_row(_translate(self.language, "cars.stopwatch_run.max_obd"), f"{max_obd:.0f} km/h")
        if max_gps is not None:
            _add_row(_translate(self.language, "cars.stopwatch_run.max_gps"), f"{max_gps:.0f} km/h")
        if max_g is not None:
            _add_row(_translate(self.language, "cars.stopwatch_run.max_g"), f"{max_g:.3f} g")

        for target_str in sorted(targets.keys(), key=lambda s: float(s)):
            v = targets[target_str]
            obd_t = v.get("obd")
            gps_t = v.get("gps")
            parts: list[str] = []
            if obd_t is not None:
                parts.append(f"OBD {obd_t:.2f} s")
            if gps_t is not None:
                parts.append(f"GPS {gps_t:.2f} s")
            if parts:
                _add_row(f"0–{target_str} km/h", "  ·  ".join(parts))

        for range_str, v in ranges.items():
            obd_t = v.get("obd")
            gps_t = v.get("gps")
            parts = []
            if obd_t is not None:
                parts.append(f"OBD {obd_t:.2f} s")
            if gps_t is not None:
                parts.append(f"GPS {gps_t:.2f} s")
            if parts:
                _add_row(f"{range_str} km/h", "  ·  ".join(parts))

        del_btn = Gtk.Button(label=_translate(self.language, "cars.stopwatch_run.delete_title"))
        del_btn.add_css_class("destructive-action")
        del_btn.set_margin_top(8)
        del_btn.set_margin_bottom(16)
        del_btn.set_margin_start(12)
        del_btn.set_margin_end(12)
        del_btn.connect("clicked", lambda _b: self._confirm_delete_stopwatch_run(run_id))
        box.append(del_btn)

        ts = self._parse_ts(data.get("run_at"))
        title = _translate(self.language, "cars.stopwatch_run.title",
                           date=ts.strftime("%d.%m.%Y %H:%M") if ts else str(run_id))
        page = Adw.NavigationPage(child=self._wrap_sub_page(scrolled, title), title=title)
        page.set_tag(f"stopwatch-run-{run_id}")
        self._stopwatch_run_detail_page = page
        self.nav_view.push(page)

    def _confirm_delete_stopwatch_run(self, run_id: int) -> None:
        dialog = Adw.AlertDialog(
            heading=_translate(self.language, "cars.stopwatch_run.delete_title"),
            body=_translate(self.language, "cars.stopwatch_run.delete_body"),
        )
        dialog.add_response("cancel", _translate(self.language, "cars.trip.delete_cancel"))
        dialog.add_response("delete", _translate(self.language, "cars.trip.delete_confirm"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda d, r: self._do_delete_stopwatch_run(run_id) if r == "delete" else None)
        dialog.present(self)

    def _do_delete_stopwatch_run(self, run_id: int) -> None:
        if self.db is None:
            return
        self.db.delete_stopwatch_run(run_id)
        self.nav_view.pop()
        self._render_detail()

    def refresh_if_showing_car(self, car_id: int) -> None:
        if self._selected_car_id == car_id and self._detail_pushed and self._selected_category == "stopwatch_runs":
            self._render_detail()
