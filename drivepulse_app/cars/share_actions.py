"""Share button handlers for CarsPage.

The single share button in the header switches role depending on which
multi-select mode (trips / scans / runs / photos) is active. When nothing is
selected it shares the whole vehicle. Per-row share calls (one trip, one scan,
one run) live here too.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gi.repository import Adw

from drivepulse_app.common import _translate

if TYPE_CHECKING:
    from drivepulse_app.db import DriveDB


class CarsShareActionsMixin:
    # Concrete CarsPage state surfaced to this mixin. See
    # project_mixin_typing.md.
    language: str
    db: DriveDB | None
    _selected_car_id: int | None
    _selected_source: str | None
    _trip_select_mode: bool
    _trip_selected_ids: set[int]
    _scan_select_mode: bool
    _scan_selected_ids: set[int]
    _run_select_mode: bool
    _run_selected_ids: set[int]
    _photo_select_mode: bool
    _photo_selected_ids: set[int]

    get_sync_client: Callable[[], Any] | None
    _exit_trip_select_mode: Callable[[], None]
    _exit_scan_select_mode: Callable[[], None]
    _exit_run_select_mode: Callable[[], None]
    _exit_photo_select_mode: Callable[[], None]

    def _on_share_btn_clicked(self) -> None:
        if self._trip_select_mode:
            self._share_selected_trips()
        elif self._scan_select_mode:
            self._share_selected_scans()
        elif self._run_select_mode:
            self._share_selected_runs()
        elif self._photo_select_mode:
            self._share_selected_photos()
        else:
            self._share_vehicle()

    def _share_vehicle(self) -> None:
        if self.db is None or self._selected_source is None:
            return
        from drivepulse_app.share.flow import ShareFlow
        ShareFlow(self, self.db, self.language, self.get_sync_client).share_vehicle(
            self._selected_source, self._selected_car_id
        )

    def _share_trip(self, trip_id: int) -> None:
        dialog = Adw.AlertDialog(
            heading=_translate(self.language, "share.trip_confirm_title"),
            body=_translate(self.language, "share.trip_confirm_body"),
        )
        dialog.add_response("cancel", _translate(self.language, "share.cancel"))
        dialog.add_response("send", _translate(self.language, "share.send"))
        dialog.set_response_appearance("send", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("send")
        dialog.set_close_response("cancel")

        def _on_response(_d: Adw.AlertDialog, resp: str) -> None:
            if resp != "send":
                return
            if self.db is None:
                return
            from drivepulse_app.share.flow import ShareFlow
            ShareFlow(self, self.db, self.language, self.get_sync_client).share_trips(
                self._selected_car_id, [trip_id]
            )

        dialog.connect("response", _on_response)
        dialog.present(self)

    def _share_selected_trips(self) -> None:
        ids = list(self._trip_selected_ids)
        self._exit_trip_select_mode()
        if self.db is None:
            return
        from drivepulse_app.share.flow import ShareFlow
        ShareFlow(self, self.db, self.language, self.get_sync_client).share_trips(
            self._selected_car_id, ids
        )

    def _share_selected_scans(self) -> None:
        ids = list(self._scan_selected_ids)
        self._exit_scan_select_mode()
        if self.db is None:
            return
        from drivepulse_app.share.flow import ShareFlow
        ShareFlow(self, self.db, self.language, self.get_sync_client).share_scans(
            self._selected_car_id, ids
        )

    def _share_selected_runs(self) -> None:
        ids = list(self._run_selected_ids)
        self._exit_run_select_mode()
        if self.db is None:
            return
        from drivepulse_app.share.flow import ShareFlow
        ShareFlow(self, self.db, self.language, self.get_sync_client).share_runs(
            self._selected_car_id, ids
        )

    def _share_selected_photos(self) -> None:
        ids = list(self._photo_selected_ids)
        self._exit_photo_select_mode()
        if self.db is None:
            return
        from drivepulse_app.share.flow import ShareFlow
        ShareFlow(self, self.db, self.language, self.get_sync_client).share_photos(
            self._selected_car_id, ids
        )

    def _share_run(self, run_id: int) -> None:
        if self.db is None:
            return
        from drivepulse_app.share.flow import ShareFlow
        ShareFlow(self, self.db, self.language, self.get_sync_client).share_run(
            self._selected_car_id, run_id
        )

    def _share_scan(self, scan_id: int) -> None:
        if self.db is None:
            return
        from drivepulse_app.share.flow import ShareFlow
        ShareFlow(self, self.db, self.language, self.get_sync_client).share_scan(
            self._selected_car_id, scan_id
        )
