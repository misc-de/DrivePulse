"""Settings dialog rows for the VIN-decoder providers (auto.dev counter,
vindecoder.eu credentials, NHTSA toggle). NHTSA is a simple bool stored
elsewhere; the auto.dev counter pulls live usage from the API."""
from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw

from drivepulse_app.common import _translate
from drivepulse_app.settings._autodev_usage import autodev_counter_subtitle


class SettingsVinDecoderMixin:
    # Concrete SettingsDialog state surfaced to this mixin. See
    # project_mixin_typing.md.
    language: str
    _autodev_month: str
    _autodev_month_count: int
    _autodev_usage_used: int
    _autodev_usage_limit: int
    _autodev_usage_paid: int
    _autodev_usage_plan: str

    on_vindecoder_api_key_changed: Callable[[str], None] | None
    on_vindecoder_secret_key_changed: Callable[[str], None] | None
    on_autodev_api_key_changed: Callable[[str], None] | None

    def _build_autodev_counter_row(self) -> Adw.ActionRow:
        # Prefer the live X-Usage-* numbers auto.dev returns on every call; fall
        # back to the locally counted monthly value when we've never spoken to
        # the server (no api key yet, all calls failed, …).
        subtitle = autodev_counter_subtitle(
            self.language,
            usage_used=self._autodev_usage_used,
            usage_limit=self._autodev_usage_limit,
            usage_paid=self._autodev_usage_paid,
            usage_plan=self._autodev_usage_plan,
            month_count=self._autodev_month_count,
            month_key=self._autodev_month,
        )
        row = Adw.ActionRow(
            title=_translate(self.language, "settings.vin_decoder.autodev.requests"),
            subtitle=subtitle,
        )
        return row

    def _on_autodev_key_changed(self, row: Adw.EntryRow) -> None:
        if self.on_autodev_api_key_changed is not None:
            self.on_autodev_api_key_changed(row.get_text().strip())

    def _on_vd_api_key_changed(self, row: Adw.EntryRow) -> None:
        if self.on_vindecoder_api_key_changed is not None:
            self.on_vindecoder_api_key_changed(row.get_text().strip())

    def _on_vd_secret_changed(self, row: Adw.EntryRow) -> None:
        if self.on_vindecoder_secret_key_changed is not None:
            self.on_vindecoder_secret_key_changed(row.get_text().strip())
