"""Tests for the monthly endpoint processing, statistics, and days-covered estimate."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.conftest import load_fixture

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import SimpleNamespace

    from custom_components.watercare.sensor import WatercareUsageSensor


@pytest.fixture
def response() -> str:
    return load_fixture("monthly_readings.json")


def test_days_in_monthly_reading_subtracts_missing_days(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    """Regression: July 2026, missingDays=24 -> 7 days, matching the live API probe."""
    sensor = make_sensor(endpoint="monthly")

    days = sensor._days_in_monthly_reading(
        {"timestamp": "2026-07-31T12:00:00.000Z", "numberOfMissingDays": 24}
    )

    assert days == 7


def test_days_in_monthly_reading_full_month_no_missing_days(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor(endpoint="monthly")

    days = sensor._days_in_monthly_reading(
        {"timestamp": "2026-06-30T12:00:00.000Z", "numberOfMissingDays": 0}
    )

    assert days == 30


async def test_process_monthly_data_uses_latest_by_timestamp_not_list_order(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    """State must come from the latest timestamp, not the last list element."""
    sensor = make_sensor(endpoint="monthly")
    # Deliberately out of chronological order.
    readings = [
        {
            "timestamp": "2026-07-31T12:00:00.000Z",
            "litres": 661,
            "numberOfMissingDays": 24,
            "statistics": {
                "currentPeriodAverage": 94,
                "differenceToPreviousPeriod": 5,
                "efficiency": {"currentHouseholdBand": 1, "usageToLowerBand": 0},
            },
        },
        {
            "timestamp": "2026-05-31T12:00:00.000Z",
            "litres": 17500,
            "numberOfMissingDays": 0,
            "statistics": {
                "currentPeriodAverage": 110,
                "differenceToPreviousPeriod": -3,
                "efficiency": {"currentHouseholdBand": 1, "usageToLowerBand": 0},
            },
        },
    ]

    await sensor.process_monthly_data(json.dumps(readings))

    assert sensor.state == 661
    assert sensor.extra_state_attributes["month_ending"] == "2026-07-31T12:00:00.000Z"
    assert sensor.extra_state_attributes["number_of_missing_days"] == 24


async def test_process_monthly_data_sets_state_class_measurement(
    make_sensor: Callable[..., WatercareUsageSensor], response: str
) -> None:
    sensor = make_sensor(endpoint="monthly")

    await sensor.process_monthly_data(response)

    assert sensor.state_class == "measurement"


async def test_generate_monthly_statistics_running_sum_and_distinct_ids(
    make_sensor: Callable[..., WatercareUsageSensor],
    response: str,
    patch_recorder: SimpleNamespace,
) -> None:
    sensor = make_sensor(endpoint="monthly")

    await sensor.process_monthly_data(response)

    consumption_points = patch_recorder.written["watercare:monthly_consumption"]
    # Fixture is already chronological: May (17500) then July (661).
    assert [p["sum"] for p in consumption_points] == [17500, 18161]

    # Must not collide with mechanicalmonthly's generic IDs.
    assert "watercare:water_consumption" not in patch_recorder.written
    assert "watercare:monthly_cost" in patch_recorder.written
    assert "watercare:monthly_consumption_cost" in patch_recorder.written
    assert "watercare:monthly_wastewater_cost" in patch_recorder.written


async def test_process_monthly_data_handles_none_response(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor(endpoint="monthly")

    await sensor.process_monthly_data(None)

    assert sensor.state is None


async def test_process_monthly_data_handles_malformed_json(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor(endpoint="monthly")
    sensor._state = 123.45  # seed a sentinel to prove it's left untouched

    await sensor.process_monthly_data("not json")

    assert sensor.state == 123.45
