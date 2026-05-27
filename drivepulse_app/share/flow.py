"""GTK UI flow for initiating a share operation."""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from drivepulse_app.db import DriveDB
from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)


class ShareFlow:
    def __init__(
        self,
        parent_widget: Gtk.Widget,
        db: DriveDB,
        language: str,
        get_client_fn: Callable | None = None,
    ) -> None:
        self._parent = parent_widget
        self._db = db
        self._language = language
        self._get_client_fn = get_client_fn

    def _t(self, key: str) -> str:
        from drivepulse_app.common import _translate
        return _translate(self._language, key)

    def _t_fmt(self, key: str, **values: object) -> str:
        from drivepulse_app.common import _translate
        return _translate(self._language, key, **values)

    def _show_toast(self, msg: str) -> None:
        root = self._parent.get_root()
        if root is not None and hasattr(root, "add_toast"):
            root.add_toast(Adw.Toast(title=msg))

    def _get_client(self) -> Any:
        if self._get_client_fn is None:
            return None
        return self._get_client_fn()

    def _get_car_row(self, car_id: int) -> Any:
        return self._db.get_car(car_id)

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def share_vehicle(self, source: str, car_id: int | None) -> None:
        client = self._get_client()
        if client is None:
            self._show_toast(self._t("share.no_sync"))
            return
        if car_id is None:
            self._show_toast(self._t("share.no_car"))
            return
        car = self._get_car_row(car_id)
        if car is None or not car["vin_hash"]:
            self._show_toast(self._t("share.no_vin"))
            return
        self._check_and_proceed_vehicle(client, car, mode="vehicle")

    def share_trips(self, car_id: int | None, trip_ids: list[int]) -> None:
        client = self._get_client()
        if client is None:
            self._show_toast(self._t("share.no_sync"))
            return
        if car_id is None:
            return
        car = self._get_car_row(car_id)
        if car is None or not car["vin_hash"]:
            self._show_toast(self._t("share.no_vin"))
            return
        self._check_and_proceed_vehicle(client, car, mode="trips", trip_ids=trip_ids)

    def share_run(self, car_id: int | None, run_id: int) -> None:
        client = self._get_client()
        if client is None:
            self._show_toast(self._t("share.no_sync"))
            return
        if car_id is None:
            return
        car = self._get_car_row(car_id)
        if car is None or not car["vin_hash"]:
            self._show_toast(self._t("share.no_vin"))
            return
        self._check_and_proceed_vehicle(client, car, mode="run", run_ids=[run_id])

    def share_scan(self, car_id: int | None, scan_id: int) -> None:
        client = self._get_client()
        if client is None:
            self._show_toast(self._t("share.no_sync"))
            return
        if car_id is None:
            return
        car = self._get_car_row(car_id)
        if car is None or not car["vin_hash"]:
            self._show_toast(self._t("share.no_vin"))
            return
        self._check_and_proceed_vehicle(client, car, mode="scan", scan_ids=[scan_id])

    def share_scans(self, car_id: int | None, scan_ids: list[int]) -> None:
        client = self._get_client()
        if client is None:
            self._show_toast(self._t("share.no_sync"))
            return
        if car_id is None or not scan_ids:
            return
        car = self._get_car_row(car_id)
        if car is None or not car["vin_hash"]:
            self._show_toast(self._t("share.no_vin"))
            return
        self._check_and_proceed_vehicle(client, car, mode="scan", scan_ids=scan_ids)

    def share_tour(self, tour: dict) -> None:
        self.share_tours([tour])

    def share_tours(self, tours: list[dict]) -> None:
        client = self._get_client()
        if client is None:
            self._show_toast(self._t("share.no_sync"))
            return
        if not tours:
            return

        def _bg() -> None:
            try:
                from drivepulse_app.share.protocol import build_tour_payload
                payload = {
                    "version": 1,
                    "type": "share_tours",
                    "tours": [build_tour_payload(t) for t in tours],
                }
                result = client.share_import(payload)
                GLib.idle_add(_on_result, result)
            except Exception as exc:
                log.exception("Tour share failed")
                GLib.idle_add(self._show_toast, self._t_fmt("share.error", detail=str(exc)))

        def _on_result(result: dict | None) -> bool:
            if result is None or not result.get("ok"):
                err = result.get("error", "?") if isinstance(result, dict) else "?"
                self._show_toast(self._t_fmt("share.error", detail=str(err)))
                return False
            if result.get("queued"):
                self._show_toast(self._t("share.queued"))
                return False
            n = result.get("tours_added", 0)
            if n:
                self._show_toast(self._t_fmt("share.tours_sent", count=str(n)))
            else:
                self._show_toast(self._t("share.nothing_new"))
            return False

        threading.Thread(target=_bg, daemon=True).start()

    def share_runs(self, car_id: int | None, run_ids: list[int]) -> None:
        client = self._get_client()
        if client is None:
            self._show_toast(self._t("share.no_sync"))
            return
        if car_id is None or not run_ids:
            return
        car = self._get_car_row(car_id)
        if car is None or not car["vin_hash"]:
            self._show_toast(self._t("share.no_vin"))
            return
        self._check_and_proceed_vehicle(client, car, mode="run", run_ids=run_ids)

    def share_photos(self, car_id: int | None, photo_ids: list[int]) -> None:
        client = self._get_client()
        if client is None:
            self._show_toast(self._t("share.no_sync"))
            return
        if car_id is None or not photo_ids:
            return
        car = self._get_car_row(car_id)
        if car is None or not car["vin_hash"]:
            self._show_toast(self._t("share.no_vin"))
            return
        self._check_and_proceed_vehicle(client, car, mode="photos", photo_ids=photo_ids)

    # ------------------------------------------------------------------
    # Internal flow
    # ------------------------------------------------------------------

    def _check_and_proceed_vehicle(
        self,
        client: Any,
        car: Any,
        mode: str,
        trip_ids: list[int] | None = None,
        run_ids: list[int] | None = None,
        scan_ids: list[int] | None = None,
        photo_ids: list[int] | None = None,
    ) -> None:
        vin_hash = car["vin_hash"]

        def _bg() -> None:
            known = client.vehicle_check(vin_hash)
            GLib.idle_add(
                _on_check_result,
                known,
            )

        def _on_check_result(known: bool | None) -> bool:
            if known is None:
                self._show_toast(self._t("share.error_check"))
                return False
            # Vehicle mode merges the known/unknown question into the
            # data-selection dialog: an anonymize checkbox plus an info hint
            # about whether the peer already knows the car. Bulk-share modes
            # (specific trips/runs/...) keep the separate intermediate dialog
            # so they still get the anon choice too.
            if mode == "vehicle":
                self._show_vehicle_share_dialog(
                    client, car,
                    anon=not bool(known),
                    vehicle_known=bool(known),
                )
            elif known:
                self._proceed(client, car, anon=False,
                              mode=mode, trip_ids=trip_ids, run_ids=run_ids,
                              scan_ids=scan_ids, photo_ids=photo_ids, vehicle_known=True)
            else:
                self._show_unknown_vehicle_dialog(
                    client, car, mode=mode,
                    trip_ids=trip_ids, run_ids=run_ids, scan_ids=scan_ids,
                    photo_ids=photo_ids,
                )
            return False

        threading.Thread(target=_bg, daemon=True).start()

    def _show_unknown_vehicle_dialog(
        self,
        client: Any,
        car: Any,
        mode: str,
        trip_ids: list[int] | None,
        run_ids: list[int] | None,
        scan_ids: list[int] | None,
        photo_ids: list[int] | None = None,
    ) -> None:
        dialog = Adw.AlertDialog(
            heading=self._t("share.vehicle_unknown_title"),
            body=self._t("share.vehicle_unknown_body"),
        )

        # Anonymize toggle defaults ON for first-pairing — replaces VIN and
        # serial numbers (cal_id, CVN) with fictive values while keeping
        # brand/label intact. Opt out to introduce the real vehicle identity.
        anon_check = Gtk.CheckButton(label=self._t("share.anonymize_toggle"))
        anon_check.set_active(True)
        anon_check.set_margin_top(8)

        dialog.set_extra_child(anon_check)
        dialog.add_response("cancel", self._t("share.cancel"))
        dialog.add_response("send", self._t("share.send"))
        dialog.set_response_appearance("send", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("send")
        dialog.set_close_response("cancel")

        def _on_response(_d: Adw.AlertDialog, resp: str) -> None:
            if resp != "send":
                return
            self._proceed(
                client, car,
                anon=anon_check.get_active(),
                mode=mode, trip_ids=trip_ids, run_ids=run_ids,
                scan_ids=scan_ids, photo_ids=photo_ids, vehicle_known=False,
            )

        dialog.connect("response", _on_response)
        dialog.present(self._parent)

    def _proceed(
        self,
        client: Any,
        car: Any,
        anon: bool,
        mode: str,
        trip_ids: list[int] | None,
        run_ids: list[int] | None,
        scan_ids: list[int] | None,
        vehicle_known: bool,
        photo_ids: list[int] | None = None,
    ) -> None:
        if mode == "vehicle":
            self._show_vehicle_share_dialog(
                client, car, anon=anon, vehicle_known=vehicle_known,
            )
        else:
            self._send_payload(
                client, car, anon=anon,
                include_trips=mode == "trips",
                include_runs=mode == "run",
                include_scans=mode == "scan",
                include_photos=mode == "photos",
                trip_ids=trip_ids, run_ids=run_ids, scan_ids=scan_ids,
                photo_ids=photo_ids,
            )

    def _show_vehicle_share_dialog(
        self,
        client: Any,
        car: Any,
        anon: bool,
        vehicle_known: bool,
    ) -> None:
        dialog = Adw.AlertDialog(
            heading=self._t("share.what_to_send_title"),
        )

        peer_hint = Gtk.Label(
            label=self._t(
                "share.peer_knows_vehicle"
                if vehicle_known
                else "share.peer_unknown_vehicle"
            )
        )
        peer_hint.set_wrap(True)
        peer_hint.set_xalign(0.0)
        peer_hint.add_css_class("dim-label")

        anon_check = Gtk.CheckButton()
        anon_check.set_active(bool(anon))
        anon_label = Gtk.Label(label=self._t("share.anonymize_toggle"))
        anon_label.set_wrap(True)
        anon_label.set_xalign(0.0)
        anon_check.set_child(anon_label)

        group = Adw.PreferencesGroup()

        trips_check = Gtk.CheckButton()
        trips_check.set_active(True)
        trips_check.set_valign(Gtk.Align.CENTER)
        trips_row = Adw.ActionRow()
        trips_row.set_title(self._t("share.include_trips"))
        trips_row.add_prefix(trips_check)
        trips_row.set_activatable_widget(trips_check)
        group.add(trips_row)

        runs_check = Gtk.CheckButton()
        runs_check.set_active(True)
        runs_check.set_valign(Gtk.Align.CENTER)
        runs_row = Adw.ActionRow()
        runs_row.set_title(self._t("share.include_runs"))
        runs_row.add_prefix(runs_check)
        runs_row.set_activatable_widget(runs_check)
        group.add(runs_row)

        scans_check = Gtk.CheckButton()
        scans_check.set_active(True)
        scans_check.set_valign(Gtk.Align.CENTER)
        scans_row = Adw.ActionRow()
        scans_row.set_title(self._t("share.include_scans"))
        scans_row.add_prefix(scans_check)
        scans_row.set_activatable_widget(scans_check)
        group.add(scans_row)

        photos_check = Gtk.CheckButton()
        photos_check.set_active(False)
        photos_check.set_valign(Gtk.Align.CENTER)
        photos_row = Adw.ActionRow()
        photos_row.set_title(self._t("share.include_photos"))
        photos_row.add_prefix(photos_check)
        photos_row.set_activatable_widget(photos_check)
        group.add(photos_row)

        extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        extra.append(peer_hint)
        extra.append(anon_check)
        extra.append(group)
        dialog.set_extra_child(extra)
        dialog.add_response("cancel", self._t("share.cancel"))
        dialog.add_response("send", self._t("share.send"))
        dialog.set_response_appearance("send", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("send")
        dialog.set_close_response("cancel")

        def _on_response(_d: Adw.AlertDialog, resp: str) -> None:
            if resp != "send":
                return
            self._send_payload(
                client, car,
                anon=anon_check.get_active(),
                include_trips=trips_check.get_active(),
                include_runs=runs_check.get_active(),
                include_scans=scans_check.get_active(),
                include_photos=photos_check.get_active(),
                trip_ids=None, run_ids=None, scan_ids=None, photo_ids=None,
            )

        dialog.connect("response", _on_response)
        dialog.present(self._parent)

    def _send_payload(
        self,
        client: Any,
        car: Any,
        anon: bool,
        include_trips: bool,
        include_runs: bool,
        include_scans: bool,
        trip_ids: list[int] | None,
        run_ids: list[int] | None,
        scan_ids: list[int] | None,
        include_photos: bool = False,
        photo_ids: list[int] | None = None,
    ) -> None:
        from drivepulse_app.share.protocol import (
            build_photos_payload,
            build_runs_payload,
            build_scans_payload,
            build_trips_payload,
            build_vehicle_block,
        )

        car_id = int(car["id"])

        def _bg() -> None:
            try:
                vehicle_block = build_vehicle_block(car, anon=anon)
                payload: dict = {
                    "version": 1,
                    "type": "share",
                    "vehicle": vehicle_block,
                    "trips": [],
                    "stopwatch_runs": [],
                    "scans": [],
                    "photos": [],
                }
                if include_trips:
                    payload["trips"] = build_trips_payload(self._db, car_id, trip_ids=trip_ids)
                if include_runs:
                    payload["stopwatch_runs"] = build_runs_payload(self._db, car_id, run_ids=run_ids)
                if include_scans:
                    payload["scans"] = build_scans_payload(self._db, car_id, scan_ids=scan_ids)
                if include_photos:
                    payload["photos"] = build_photos_payload(self._db, car_id, photo_ids=photo_ids)

                result = client.share_import(payload)
                GLib.idle_add(_on_result, result)
            except Exception as exc:
                log.exception("Share send failed")
                _err = str(exc)
                GLib.idle_add(self._show_toast, self._t_fmt("share.error", detail=str(_err)))

        def _on_result(result: dict | None) -> bool:
            if result is None or not result.get("ok"):
                err = result.get("error", "?") if isinstance(result, dict) else "?"
                self._show_toast(self._t_fmt("share.error", detail=str(err)))
                return False
            if result.get("queued"):
                self._show_toast(self._t("share.queued"))
                return False
            trips_n = result.get("trips_added", 0)
            runs_n = result.get("runs_added", 0)
            scans_n = result.get("scans_added", 0)
            photos_n = result.get("photos_added", 0)
            conflicts_n = result.get("conflicts", 0)
            parts = []
            if trips_n:
                parts.append(f"{trips_n} Fahrten")
            if runs_n:
                parts.append(f"{runs_n} Läufe")
            if scans_n:
                parts.append(f"{scans_n} Scans")
            if photos_n:
                parts.append(f"{photos_n} Fotos")
            if parts:
                self._show_toast(f"Übertragen: {', '.join(parts)}")
            elif conflicts_n:
                self._show_toast(f"{conflicts_n} Konflikte")
            else:
                self._show_toast(self._t("share.nothing_new"))
            return False

        threading.Thread(target=_bg, daemon=True).start()
