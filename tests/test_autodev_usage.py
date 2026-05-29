"""Unit tests for the pure auto.dev request-counter subtitle formatter."""

from drivepulse_app.common import _translate
from drivepulse_app.settings._autodev_usage import autodev_counter_subtitle


def _sub(language="en", *, used=0, limit=0, paid=0, plan="", month_count=0, month_key=""):
    return autodev_counter_subtitle(
        language,
        usage_used=used,
        usage_limit=limit,
        usage_paid=paid,
        usage_plan=plan,
        month_count=month_count,
        month_key=month_key,
    )


def test_no_usage_and_no_month_shows_no_requests():
    assert _sub() == _translate("en", "settings.vin_decoder.autodev.no_requests")


def test_free_plan_shows_used_over_limit_ratio():
    assert _sub(used=50, limit=1000, plan="starter") == "50 / 1000 · starter"


def test_free_plan_without_name_omits_plan_part():
    # has_live because limit > 0; free framing because plan is empty.
    assert _sub(used=50, limit=1000) == "50 / 1000"


def test_paid_plan_shows_absolute_count_not_ratio():
    assert _sub(used=5000, limit=10000, plan="pro") == "5000 · pro"


def test_paid_purchases_appended():
    result = _sub(used=5000, plan="pro", paid=3)
    paid_text = _translate("en", "settings.vin_decoder.autodev.paid", n=3)
    assert result == f"5000 · pro · {paid_text}"


def test_fallback_monthly_english_month_label():
    assert _sub(month_count=7, month_key="2026-05") == "7 / 1000 · May 2026"


def test_fallback_monthly_german_month_label():
    assert _sub(language="de", month_count=7, month_key="2026-05") == "7 / 1000 · Mai 2026"


def test_fallback_monthly_invalid_key_passes_through():
    assert _sub(month_count=5, month_key="garbage") == "5 / 1000 · garbage"


def test_zero_limit_free_plan_falls_back_to_absolute():
    # plan present (so has_live) but no limit -> can't show a ratio.
    assert _sub(used=12, limit=0, plan="starter") == "12 · starter"
