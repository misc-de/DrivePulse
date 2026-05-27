"""StopWatch-run list and detail helpers for CarsPage."""
from __future__ import annotations

from typing import Any

from gi.repository import Adw, GLib, Gtk, Pango

from drivepulse_app.common import _translate
from drivepulse_app.diagnostics import get_logger

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

        keys = run.keys() if hasattr(run, "keys") else []
        shared_at = run["shared_at"] if "shared_at" in keys else None
        seen_at = run["seen_at"] if "seen_at" in keys else None
        if shared_at and not seen_at:
            dot = Gtk.Label(label="●")
            dot.add_css_class("dp-new-dot")
            dot.set_valign(Gtk.Align.CENTER)
            row.add_prefix(dot)

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

        if self._run_select_mode:
            chk = Gtk.CheckButton()
            chk.set_active(run_id in self._run_selected_ids)
            chk.set_valign(Gtk.Align.CENTER)
            chk.connect("toggled", lambda c, rid=run_id: self._on_run_checkbox_toggled(rid, c.get_active()))
            row.add_prefix(chk)
            row.set_activatable(True)
            row.connect("activated", lambda _r, c=chk: c.set_active(not c.get_active()))
        else:
            icon = Gtk.Image.new_from_icon_name("stopwatch-symbolic")
            row.add_prefix(icon)
            chev = Gtk.Image.new_from_icon_name("go-next-symbolic")
            row.add_suffix(chev)
            row.set_activatable(True)
            row.connect("activated", lambda _r, rid=run_id: self._open_stopwatch_run_detail(rid))
            lp = Gtk.GestureLongPress()
            lp.connect("pressed", lambda _g, _x, _y, rid=run_id: self._enter_run_select_mode(rid))
            row.add_controller(lp)
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
        try:
            self.db.mark_run_seen(run_id)
        except Exception:
            log.exception("Could not mark run seen id=%s", run_id)

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

        # ── Summary metric cards ──────────────────────────────────────
        metrics: list[tuple[str, str]] = []
        if max_obd is not None:
            metrics.append((_translate(self.language, "cars.stopwatch_run.max_obd"), f"{max_obd:.0f} km/h"))
        if max_gps is not None:
            metrics.append((_translate(self.language, "cars.stopwatch_run.max_gps"), f"{max_gps:.0f} km/h"))
        if max_g is not None:
            metrics.append((_translate(self.language, "cars.stopwatch_run.max_g"), f"{max_g:.2f} g"))

        if metrics:
            summary_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            summary_box.set_homogeneous(True)
            summary_box.set_margin_top(12)
            summary_box.set_margin_bottom(4)
            summary_box.set_margin_start(12)
            summary_box.set_margin_end(12)

            for cap, val in metrics:
                card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                card.add_css_class("card")
                val_lbl = Gtk.Label(label=val)
                val_lbl.add_css_class("title-2")
                val_lbl.add_css_class("monospace")
                val_lbl.set_margin_top(10)
                val_lbl.set_margin_start(8)
                val_lbl.set_margin_end(8)
                cap_lbl = Gtk.Label(label=cap)
                cap_lbl.add_css_class("caption")
                cap_lbl.add_css_class("dim-label")
                cap_lbl.set_wrap(True)
                cap_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
                cap_lbl.set_justify(Gtk.Justification.CENTER)
                cap_lbl.set_lines(2)
                cap_lbl.set_margin_bottom(10)
                cap_lbl.set_margin_start(4)
                cap_lbl.set_margin_end(4)
                card.append(val_lbl)
                card.append(cap_lbl)
                summary_box.append(card)

            box.append(summary_box)

        # ── Times table ───────────────────────────────────────────────
        group = Adw.PreferencesGroup()
        group.set_margin_top(8)
        group.set_margin_bottom(12)
        group.set_margin_start(12)
        group.set_margin_end(12)
        box.append(group)

        def _add_time_row(title: str, obd_t: float | None, gps_t: float | None) -> None:
            row = Adw.ActionRow()
            row.set_title(GLib.markup_escape_text(title))
            row.set_activatable(False)

            sfx = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
            sfx.set_valign(Gtk.Align.CENTER)

            obd_val_lbl: Gtk.Label | None = None
            gps_val_lbl: Gtk.Label | None = None

            if obd_t is not None:
                col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
                col.set_halign(Gtk.Align.END)
                src = Gtk.Label(label="OBD")
                src.add_css_class("caption")
                src.add_css_class("dim-label")
                obd_val_lbl = Gtk.Label(label=f"{obd_t:.2f} s")
                obd_val_lbl.add_css_class("monospace")
                col.append(src)
                col.append(obd_val_lbl)
                sfx.append(col)

            if gps_t is not None:
                col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
                col.set_halign(Gtk.Align.END)
                src = Gtk.Label(label="GPS")
                src.add_css_class("caption")
                src.add_css_class("dim-label")
                gps_val_lbl = Gtk.Label(label=f"{gps_t:.2f} s")
                gps_val_lbl.add_css_class("monospace")
                col.append(src)
                col.append(gps_val_lbl)
                sfx.append(col)

            if obd_val_lbl and gps_val_lbl and obd_t is not None and gps_t is not None:
                if obd_t <= gps_t:
                    obd_val_lbl.add_css_class("accent")
                else:
                    gps_val_lbl.add_css_class("accent")

            row.add_suffix(sfx)
            group.add(row)

        for target_str in sorted(targets.keys(), key=float):
            v = targets[target_str]
            obd_t = v.get("obd")
            gps_t = v.get("gps")
            if obd_t is not None or gps_t is not None:
                _add_time_row(f"0–{target_str} km/h", obd_t, gps_t)

        for range_str, v in ranges.items():
            obd_t = v.get("obd")
            gps_t = v.get("gps")
            if obd_t is not None or gps_t is not None:
                _add_time_row(f"{range_str} km/h", obd_t, gps_t)

        load_btn = Gtk.Button(label=_translate(self.language, "cars.stopwatch_run.load_in_stopwatch"))
        load_btn.add_css_class("suggested-action")
        load_btn.set_margin_top(8)
        load_btn.set_margin_bottom(4)
        load_btn.set_margin_start(12)
        load_btn.set_margin_end(12)
        load_btn.connect("clicked", lambda _b: self._load_run_in_stopwatch(data))
        box.append(load_btn)

        if not self.mock_mode:
            del_btn = Gtk.Button(label=_translate(self.language, "cars.stopwatch_run.delete_title"))
            del_btn.add_css_class("destructive-action")
            del_btn.set_margin_top(4)
            del_btn.set_margin_bottom(16)
            del_btn.set_margin_start(12)
            del_btn.set_margin_end(12)
            del_btn.connect("clicked", lambda _b: self._confirm_delete_stopwatch_run(run_id))
            box.append(del_btn)

        ts = self._parse_ts(data.get("run_at"))
        title = _translate(self.language, "cars.stopwatch_run.title",
                           date=ts.strftime("%d.%m.%Y %H:%M") if ts else str(run_id))
        page = Adw.NavigationPage(
            child=self._wrap_sub_page(
                scrolled,
                title,
                on_share=(lambda: self._share_run(run_id)) if self._is_sync_active() else None,
            ),
            title=title,
        )
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

    def _load_run_in_stopwatch(self, data: dict[str, Any]) -> None:
        """Hand off a persisted run to the StopWatch tab for replay."""
        callback = getattr(self, "on_load_stopwatch_run", None)
        if not callable(callback):
            return
        try:
            callback(data)
        except Exception:
            log.exception("Could not load stopwatch run into StopWatch tab")

    def refresh_if_showing_car(self, car_id: int) -> None:
        if self._selected_car_id == car_id and self._detail_pushed and self._selected_category == "stopwatch_runs":
            self._render_detail()

    def _enter_run_select_mode(self, run_id: int) -> None:
        self._run_select_mode = True
        self._run_selected_ids = {run_id}
        self._render_detail()
        self._update_list_select_buttons()
        GLib.idle_add(self._reapply_list_select_ui)

    def _exit_run_select_mode(self) -> None:
        self._run_select_mode = False
        self._run_selected_ids = set()
        self._render_detail()
        self._update_list_select_buttons()
        self._update_trash_default()

    def _on_run_checkbox_toggled(self, run_id: int, active: bool) -> None:
        if active:
            self._run_selected_ids.add(run_id)
        else:
            self._run_selected_ids.discard(run_id)
        if not self._run_selected_ids:
            self._exit_run_select_mode()

    def _confirm_delete_selected_runs(self) -> None:
        n = len(self._run_selected_ids)
        if n == 0:
            return
        dialog = self._make_delete_dialog("cars.stopwatch_run.delete_title", "cars.stopwatch_run.delete_title")
        dialog.set_body(_translate(self.language, "cars.trip.delete_multi_body", n=n))
        dialog.connect("response", lambda _d, r: self._delete_selected_runs() if r == "delete" else None)
        dialog.present(self)

    def _delete_selected_runs(self) -> None:
        if self.db is None:
            return
        for rid in list(self._run_selected_ids):
            try:
                self.db.delete_stopwatch_run(rid)
            except Exception:
                log.exception("Could not delete selected run id=%s", rid)
        self._exit_run_select_mode()
