"""VIN lookup flow on CarsPage: per-car prompt for newly added vehicles,
explicit-refresh worker, and the review dialog that lets the user accept or
discard the decoded fields."""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

from gi.repository import Adw, GLib

from drivepulse_app.cars.profiles import _load_profiles
from drivepulse_app.common import _translate
from drivepulse_app.diagnostics import get_logger
from drivepulse_app.vin.api import fetch_vin_data

log = get_logger(__name__)


class CarsVinFlowMixin:
    def _schedule_vin_fetches(self) -> None:
        """For each car the user has just added that still has no VIN
        data on file, queue a one-off prompt asking whether to look the
        VIN up online. The app never auto-fetches anymore — the user
        decides per car, and either the fetched data or an explicit
        decline is recorded so the prompt doesn't reappear next start.
        """
        if self.db is None:
            return
        for entry in self._profiles:
            car_id = entry.get("car_id")
            vin = entry.get("vin") or ""
            if not car_id or not vin or len(vin) < 11:
                continue
            if entry.get("vin_data_fetched"):
                # Either real data on file or the user previously declined
                # (stored as "{}") — both count as "already decided".
                continue
            if car_id in self._vin_new_car_prompted:
                continue
            if car_id in self._vin_fetch_pending:
                continue
            self._vin_new_car_prompted.add(car_id)
            self._vin_new_car_prompt_queue.append(car_id)
        self._maybe_show_next_new_car_prompt()

    def _maybe_show_next_new_car_prompt(self) -> None:
        if self._vin_new_car_prompt_open or not self._vin_new_car_prompt_queue:
            return
        car_id = self._vin_new_car_prompt_queue.pop(0)
        entry = next((e for e in self._profiles if e.get("car_id") == car_id), None)
        if entry is None or entry.get("vin_data_fetched"):
            # Profile disappeared or has been decided meanwhile — skip.
            self._maybe_show_next_new_car_prompt()
            return
        vin = str(entry.get("vin") or "")
        if not vin:
            self._maybe_show_next_new_car_prompt()
            return

        label = (
            entry.get("label")
            or entry.get("brand")
            or _translate(self.language, "cars.unknown")
        )
        vin_tail = f"…{vin[-5:]}" if len(vin) > 5 else vin
        dialog = Adw.AlertDialog()
        dialog.set_heading(_translate(self.language, "cars.vin_new_car.heading"))
        dialog.set_body(_translate(
            self.language, "cars.vin_new_car.body", label=label, vin=vin_tail,
        ))
        dialog.add_response("decline", _translate(self.language, "cars.vin_new_car.cancel"))
        dialog.add_response("fetch", _translate(self.language, "cars.vin_new_car.ok"))
        dialog.set_default_response("fetch")
        dialog.set_close_response("decline")
        dialog.set_response_appearance("fetch", Adw.ResponseAppearance.SUGGESTED)
        self._vin_new_car_prompt_open = True

        def _on_response(_d: Adw.AlertDialog, response: str) -> None:
            self._vin_new_car_prompt_open = False
            if response == "fetch":
                # Reuse the explicit-refresh pipeline — its progress
                # dialog + review flow is exactly what we want here.
                self._start_vin_refresh(car_id, vin, entry)
            else:
                # User declined for this car — write an empty marker so
                # vin_data_fetched flips to True and we never re-ask.
                if self.db is not None:
                    try:
                        self.db.update_car_vin_data(car_id, "{}")
                    except sqlite3.Error:
                        log.warning("Could not persist decline for car_id=%s",
                                    car_id, exc_info=True)
                try:
                    self._profiles = _load_profiles(self.db)
                except Exception:
                    log.debug("Could not reload profiles after decline", exc_info=True)
            # Drain the next prompt if any cars are still pending.
            self._maybe_show_next_new_car_prompt()

        dialog.connect("response", _on_response)
        root = self.get_root()
        if root:
            dialog.present(root)
        else:
            # No window yet — defer to the next idle.
            GLib.idle_add(lambda: (dialog.present(self), False)[1])

    def _start_refetch_with_dialog(self, car_id: int, vin: str, dialog: Any) -> None:
        threading.Thread(
            target=self._refetch_vin_with_dialog_thread,
            args=(car_id, vin, dialog),
            daemon=True,
        ).start()

    def _refetch_vin_with_dialog_thread(self, car_id: int, vin: str, dialog: Any) -> None:
        def _on_source_done(source: str, ok: bool, error_code: str, field_count: int) -> None:
            if not ok:
                if error_code == "auth":
                    msg = _translate(self.language, "vin.autodev.error.auth")
                elif error_code == "not_found":
                    msg = _translate(self.language, "vin.autodev.error.not_found")
                elif error_code == "vin_format":
                    msg = _translate(self.language, "vin.autodev.error.vin_format")
                else:
                    msg = _translate(self.language, "vin.autodev.error.generic")
            else:
                msg = ""
            GLib.idle_add(dialog.set_source_result, source, ok, msg, field_count)

        try:
            data = fetch_vin_data(
                vin,
                autodev_api_key=self._autodev_api_key,
                vindecoder_api_key=self._vindecoder_api_key,
                vindecoder_secret_key=self._vindecoder_secret_key,
                nhtsa_enabled=self._nhtsa_enabled,
                on_autodev_call=self._on_autodev_call,
                on_source_done=_on_source_done,
            )
        except Exception:
            log.warning("VIN refetch failed for car_id=%s", car_id, exc_info=True)
            data = {}
        GLib.idle_add(self._on_vin_refetch_dialog_done, car_id, vin, data, dialog)

    def _on_vin_refetch_dialog_done(
        self, car_id: int, vin: str, sources: dict, dialog: Any
    ) -> bool:
        self._vin_fetch_pending.discard(car_id)
        raw = sources.pop("auto.dev_raw", None)
        if raw and self.db is not None:
            try:
                self.db.save_autodev_raw(car_id, raw)
            except Exception:
                log.warning("Could not save autodev raw for car_id=%s", car_id, exc_info=True)
        sources.pop("auto.dev_error", None)  # already shown in dialog row
        # The user already confirmed they want to refresh in the
        # pre-confirm dialog — a second "Weiter"-click on the progress
        # dialog after the fetch finishes is redundant. Auto-close it
        # and route the result to the review (or no-changes toast)
        # immediately.
        try:
            dialog.close()
        except Exception:
            log.debug("Could not auto-close VIN fetch dialog", exc_info=True)
        if sources:
            self._vin_review_queue.append((car_id, vin, sources))
            self._maybe_show_next_review()
        else:
            # Nothing came back — leave existing vin_data_json alone,
            # drop the snapshot and tell the user.
            self._vin_refetch_existing.pop(car_id, None)
            self._show_toast(_translate(self.language, "cars.vin_refresh.no_changes"))
        return False

    def queue_vin_reviews(self, reviews: list) -> None:
        """Enqueue VIN review items arriving from a sync operation.

        Each item: {"car_id": int, "vin": str, "fields": {field: value}}.
        New fields are shown using the existing review dialog so the user
        can accept or reject each one with a checkbox.
        """
        for item in reviews:
            car_id = item.get("car_id")
            vin = item.get("vin") or ""
            fields = item.get("fields")
            if not car_id or not isinstance(fields, dict) or not fields:
                continue
            sources = {"sync": fields}
            self._vin_review_queue.append((car_id, vin, sources))
        if reviews:
            self._maybe_show_next_review()

    def _show_toast(self, msg: str) -> None:
        root = self.get_root()
        if root and hasattr(root, "add_toast"):
            root.add_toast(Adw.Toast(title=msg))

    def _maybe_show_next_review(self) -> None:
        if self._vin_review_open or not self._vin_review_queue:
            return
        car_id, vin, data = self._vin_review_queue.pop(0)

        # Drop fields that match what we already have on file — the user
        # asked the refresh to surface *new* data, not re-confirm the
        # full record every time. Sources that end up empty after the
        # filter are removed; if no source has anything new at all, show
        # a toast and skip the review dialog entirely.
        existing = self._vin_refetch_existing.get(car_id, {})
        if existing:
            filtered: dict = {}
            for src_name, src_data in data.items():
                kept = {
                    field: value
                    for field, value in (src_data or {}).items()
                    if existing.get(field) != value
                }
                if kept:
                    filtered[src_name] = kept
            data = filtered
        if not data:
            self._show_toast(_translate(self.language, "cars.vin_refresh.no_changes"))
            self._vin_refetch_existing.pop(car_id, None)
            # Drain anything else that already queued up while we were
            # filtering — keeps the UX consistent across multi-car refreshes.
            self._maybe_show_next_review()
            return

        self._vin_review_open = True
        from drivepulse_app.vin.review_dialog import VinReviewDialog
        dialog = VinReviewDialog(vin, data, self.language)

        def _on_response(d: VinReviewDialog, response: str) -> None:
            self._vin_review_open = False
            existing_snap = self._vin_refetch_existing.pop(car_id, {})
            if response != "accept":
                # Cancel: leave the DB alone. Crucially we do NOT write
                # the snapshot back — if it was somehow empty (defensive)
                # that would wipe perfectly good existing data.
                self._profiles = _load_profiles(self.db)
                self._rebuild_list()
                if self._detail_pushed:
                    self._render_detail()
                self._maybe_show_next_review()
                return
            accepted = d.get_accepted_data()
            # Merge accepted fields into the snapshot so existing values
            # the user already curated stay intact when the refresh only
            # touches a subset of fields.
            merged = {**existing_snap, **accepted}
            if self.db is not None:
                try:
                    self.db.update_car_vin_data(car_id, json.dumps(merged))
                except sqlite3.Error:
                    log.warning("Could not persist VIN data for car_id=%s", car_id, exc_info=True)
                # Promote the decoded manufacturer into the permanent brand
                # column when the car has none yet — brand is the single
                # non-editable manufacturer field shown to the user and
                # survives later vin_data_json resets.
                decoded_manufacturer = (accepted.get("manufacturer") or "").strip()
                if decoded_manufacturer:
                    try:
                        existing = self.db.get_car(car_id)
                        if existing is not None and not (existing["brand"] or "").strip():
                            self.db.update_car_brand(car_id, decoded_manufacturer)
                    except sqlite3.Error:
                        log.warning(
                            "Could not promote VIN manufacturer to brand for car_id=%s",
                            car_id, exc_info=True,
                        )
            self._profiles = _load_profiles(self.db)
            self._rebuild_list()
            if self._detail_pushed:
                self._render_detail()
            self._maybe_show_next_review()

        dialog.connect("response", _on_response)
        root = self.get_root()
        if root:
            dialog.present(root)
