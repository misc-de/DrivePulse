"""Pure formatting for the auto.dev request-counter subtitle.

The settings row shows how many VIN-decoder requests have been used. The wording
depends on whether auto.dev returned live ``X-Usage-*`` headers, whether the
plan is the free tier (where a "used / limit" ratio is meaningful) and, as a
fallback, on the locally counted monthly total. That branching string logic is
pure, so it lives here for unit testing instead of inside the GTK row builder.
"""

from __future__ import annotations

from datetime import datetime

from drivepulse_app.common import _translate

# Free tiers where the "used / limit" framing is meaningful.
_FREE_PLANS = ("starter", "free", "hobby")

_MONTHS_DE = [
    "", "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]


def autodev_counter_subtitle(
    language: str,
    *,
    usage_used: int,
    usage_limit: int,
    usage_paid: int,
    usage_plan: str,
    month_count: int,
    month_key: str,
) -> str:
    """Build the auto.dev counter row subtitle.

    Prefers the live ``X-Usage-*`` numbers; falls back to the locally counted
    monthly value (``count / 1000 · <month>``) when the server has never been
    reached. Paid plans show just the absolute used count rather than a ratio.
    """
    plan = usage_plan or ""
    is_free_plan = (not plan) or plan.lower() in _FREE_PLANS
    has_live = usage_used > 0 or usage_limit > 0 or bool(plan)
    if has_live:
        parts: list[str] = []
        if is_free_plan and usage_limit > 0:
            parts.append(f"{usage_used} / {usage_limit}")
        else:
            parts.append(str(usage_used))
        if plan:
            parts.append(plan)
        if usage_paid > 0:
            parts.append(_translate(language, "settings.vin_decoder.autodev.paid", n=usage_paid))
        return " · ".join(parts)

    if month_key:
        try:
            dt = datetime.strptime(month_key, "%Y-%m")
            if language.startswith("de"):
                month_label = f"{_MONTHS_DE[dt.month]} {dt.year}"
            else:
                month_label = dt.strftime("%B %Y")
        except ValueError:
            month_label = month_key
        return f"{month_count} / 1000 · {month_label}"

    return _translate(language, "settings.vin_decoder.autodev.no_requests")
