"""Settings dialog rows for the VIN-decoder providers (auto.dev counter,
vindecoder.eu credentials, NHTSA toggle). NHTSA is a simple bool stored
elsewhere; the auto.dev counter pulls live usage from the API."""
from __future__ import annotations

from datetime import datetime

from gi.repository import Adw

from drivepulse_app.common import _translate


class SettingsVinDecoderMixin:
    def _build_autodev_counter_row(self) -> Adw.ActionRow:
        # Prefer the live X-Usage-* numbers auto.dev returns on every call.
        # Fall back to the locally counted monthly value when we've never
        # spoken to the server (no api key yet, all calls failed, …).
        live_used = self._autodev_usage_used
        live_limit = self._autodev_usage_limit
        live_paid = self._autodev_usage_paid
        plan = self._autodev_usage_plan or ""
        # "Starter" is auto.dev's free 1000/month tier; only for free plans
        # does the "used / limit" framing actually mean something. Paid
        # plans either bill per-call or include large allowances where the
        # ratio is mostly meaningless — show just the absolute number.
        is_free_plan = (not plan) or plan.lower() in ("starter", "free", "hobby")
        has_live = live_used > 0 or live_limit > 0 or bool(plan)
        if has_live:
            parts: list[str] = []
            if is_free_plan and live_limit > 0:
                parts.append(f"{live_used} / {live_limit}")
            else:
                parts.append(str(live_used))
            if plan:
                parts.append(plan)
            if live_paid > 0:
                parts.append(
                    _translate(self.language, "settings.vin_decoder.autodev.paid",
                               n=live_paid)
                )
            subtitle = " · ".join(parts)
        else:
            count = self._autodev_month_count
            month_key = self._autodev_month
            if month_key:
                try:
                    dt = datetime.strptime(month_key, "%Y-%m")
                    if self.language.startswith("de"):
                        _MONTHS_DE = [
                            "", "Januar", "Februar", "März", "April", "Mai", "Juni",
                            "Juli", "August", "September", "Oktober", "November", "Dezember",
                        ]
                        month_label = f"{_MONTHS_DE[dt.month]} {dt.year}"
                    else:
                        month_label = dt.strftime("%B %Y")
                except ValueError:
                    month_label = month_key
                subtitle = f"{count} / 1000 · {month_label}"
            else:
                subtitle = _translate(self.language, "settings.vin_decoder.autodev.no_requests")
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
