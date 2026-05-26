"""Map page tour topnav (Load/Plan/Save/History) and the bulk-share dispatch
that is shared across the saved-tour list and the tour/trip history list."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from gi.repository import Adw, GLib, Gtk

from drivepulse_app.common import _translate
from drivepulse_app.diagnostics import get_logger
from drivepulse_app.map.layout_css import _install_maneuver_css

log = get_logger(__name__)


class MapTourActionsMixin:
    """Top navigation bar above the map plus the bulk-share helpers reused by
    the saved-tour and history lists."""

    # Declared here so mypy widens the inferred attribute type across the mixin
    # chain. Owning class (MapPage) initialises the concrete value in __init__.
    _loaded_tour_name: str | None

    def _build_tour_topnav(self) -> None:
        _install_maneuver_css()
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        bar.add_css_class("dp-tour-topnav")
        bar.set_margin_start(4)
        bar.set_margin_end(4)
        bar.set_margin_top(4)
        bar.set_margin_bottom(4)
        self._tour_topnav = bar

        def _child(icon_name: str, label_key: str) -> Gtk.Box:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.set_halign(Gtk.Align.CENTER)
            img = Gtk.Image.new_from_icon_name(icon_name)
            img.set_pixel_size(22)
            lbl = Gtk.Label(label=_translate(self.language, label_key))
            lbl.add_css_class("caption")
            box.append(img)
            box.append(lbl)
            return box

        load_btn = Gtk.Button()
        load_btn.set_child(_child("document-open-symbolic", "map.topnav.load"))
        load_btn.add_css_class("flat")
        load_btn.set_hexpand(True)
        load_btn.connect("clicked", self._on_tour_load_clicked)
        self._tour_load_btn = load_btn

        plan_btn = Gtk.ToggleButton()
        plan_btn.set_child(_child("distance-symbolic", "map.topnav.plan"))
        plan_btn.add_css_class("flat")
        plan_btn.set_hexpand(True)
        plan_btn.connect("toggled", self._on_tour_plan_toggled)
        self._tour_plan_btn = plan_btn

        save_btn = Gtk.Button()
        save_btn.set_child(_child("document-save-symbolic", "map.topnav.save"))
        save_btn.add_css_class("flat")
        save_btn.set_hexpand(True)
        save_btn.connect("clicked", self._on_tour_save_clicked)
        save_btn.set_visible(False)
        self._tour_save_btn = save_btn

        history_btn = Gtk.Button()
        history_btn.set_child(_child("document-open-recent-symbolic", "map.topnav.history"))
        history_btn.add_css_class("flat")
        history_btn.set_hexpand(True)
        history_btn.connect("clicked", self._on_tour_history_clicked)
        self._tour_history_btn = history_btn

        # "Letzte Touren" sits on the far left as a view-only entry point;
        # the tour-planning actions (load / plan / save) follow on the right.
        for btn in (history_btn, load_btn, plan_btn, save_btn):
            bar.append(btn)

        self._map_content_box.append(bar)

    def _on_tour_plan_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._tour_plan_active = btn.get_active()
        if self._search_bar is not None:
            self._search_bar.set_visible(self._tour_plan_active)
        # Keep an already-loaded tour visible on the map while the user edits
        # waypoints; the route is only cleared once "Calculate route" runs
        # (see _on_route_clicked, which clears overlays before re-routing).
        GLib.idle_add(self._nudge_map_resize)

    # ── Bulk share from select mode ──────────────────────────────────────────

    def _sync_active(self) -> bool:
        """True when the sync client is configured and available — gates the
        bulk share-button. Mirrors the per-row gate in _make_saved_tour_row."""
        sync_getter = getattr(self, "get_sync_client", None)
        return callable(sync_getter) and sync_getter() is not None

    def notify_sync_changed(self) -> None:
        """Sync-Status hat sich geändert — Tour-Listen und Share-Buttons neu aufbauen."""
        try:
            self._rebuild_tour_list()
            self._rebuild_tour_history_rows()
        except Exception:
            pass

    def _make_share_flow(self) -> Any:
        from drivepulse_app.share.flow import ShareFlow
        return ShareFlow(
            self, self._map_db, self.language, getattr(self, "get_sync_client", None)
        )

    def _on_saved_tour_share_clicked(self, _btn: Gtk.Button) -> None:
        ids = list(getattr(self, "_saved_tour_selected", []))
        if not ids:
            return
        # Resolve full tour rows from the list of metas built in _rebuild_tour_list.
        id_set = set(ids)
        tours = [t for t in self._saved_tour_metas if int(t["id"]) in id_set]
        if not tours:
            return

        def _send_saved_tours() -> None:
            self._make_share_flow().share_tours(tours)
            self._exit_saved_tour_select_mode()

        self._confirm_and_bulk_share(
            count=len(tours),
            on_send=_send_saved_tours,
        )

    def _on_history_share_clicked(self, _btn: Gtk.Button) -> None:
        selected = getattr(self, "_tour_history_selected", None)
        if not selected:
            return
        key_set = set(selected)
        metas = [
            m for m in self._tour_history_metas
            if (m["kind"], int(m["id"])) in key_set
        ]
        if not metas:
            return

        # Split into saved-tours (one batched payload) and trips (one batch
        # per owning car_id, since share_trips runs the per-vehicle handshake).
        tour_ids = [int(m["id"]) for m in metas if m["kind"] == "tour"]
        trips_by_car: dict[int, list[int]] = {}
        for m in metas:
            if m["kind"] != "trip":
                continue
            cid = m.get("car_id")
            if cid is None:
                continue
            trips_by_car.setdefault(int(cid), []).append(int(m["id"]))

        if not tour_ids and not trips_by_car:
            return

        def _do_send() -> None:
            flow = self._make_share_flow()
            if tour_ids:
                db = getattr(self, "_map_db", None)
                tour_rows: list[dict] = []
                if db is not None:
                    for tid in tour_ids:
                        row = db.get_saved_tour(tid)
                        if row is None:
                            continue
                        tour_rows.append({
                            "id": row["id"],
                            "name": row["name"],
                            "created_at": row["created_at"],
                            "waypoints_json": row["waypoints_json"],
                        })
                if tour_rows:
                    flow.share_tours(tour_rows)
            for car_id, trip_ids in trips_by_car.items():
                flow.share_trips(car_id, trip_ids)
            self._exit_history_select_mode()

        self._confirm_and_bulk_share(count=len(metas), on_send=_do_send)

    def _confirm_and_bulk_share(self, count: int, on_send: Callable[[], object]) -> None:
        dialog = Adw.AlertDialog(
            heading=_translate(self.language, "share.selected_confirm_heading"),
            body=_translate(self.language, "share.selected_confirm_body", count=str(count)),
        )
        dialog.add_response("cancel", _translate(self.language, "share.cancel"))
        dialog.add_response("send", _translate(self.language, "share.send"))
        dialog.set_response_appearance("send", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("send")
        dialog.set_close_response("cancel")

        def _on_response(_d: Adw.AlertDialog, resp: str) -> None:
            if resp == "send":
                on_send()

        dialog.connect("response", _on_response)
        dialog.present(self.get_root())
