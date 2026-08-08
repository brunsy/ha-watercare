"""Tests for WatercareUsageSensor._calculate_cost."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from custom_components.watercare.sensor import WatercareUsageSensor


def test_calculate_cost_breakdown(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor(
        endpoint="mechanicalmonthly",
        consumption_rate=2.0,
        wastewater_rate=4.0,
        wastewater_ratio=0.5,
        annual_line_charge=365,
    )

    breakdown = sensor._calculate_cost(1000, 10)

    assert breakdown["consumption"] == 2.0
    assert breakdown["wastewater"] == 2.0
    # 365/365 * 10 days = 10, computed from the actual annual_line_charge
    # and number_of_days -- not the hardcoded DEFAULT_ANNUAL_LINE_CHARGE.
    assert breakdown["line_charge"] == 10.0
    assert (
        breakdown["total"]
        == breakdown["consumption"] + breakdown["wastewater"] + breakdown["line_charge"]
    )


def test_calculate_cost_line_charge_uses_configured_annual_charge(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    """Regression: line_charge uses annual_line_charge, not the hardcoded default."""
    non_default_charge = 730  # deliberately different from DEFAULT_ANNUAL_LINE_CHARGE
    sensor = make_sensor(
        endpoint="mechanicalmonthly", annual_line_charge=non_default_charge
    )

    breakdown = sensor._calculate_cost(0, 365)

    assert breakdown["line_charge"] == non_default_charge


def test_calculate_cost_zero_usage_still_prorates_line_charge(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor(endpoint="mechanicalmonthly", annual_line_charge=365)

    breakdown = sensor._calculate_cost(0, 30)

    assert breakdown["consumption"] == 0
    assert breakdown["wastewater"] == 0
    assert breakdown["line_charge"] == 30.0
