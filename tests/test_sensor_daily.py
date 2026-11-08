"""Tests for process_daily_data (dailywithstats endpoint)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.conftest import load_fixture, nz_timestamp

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import SimpleNamespace

    from custom_components.watercare.sensor import WatercareUsageSensor


@pytest.fixture
def relative_response() -> str:
    """Return a dailywithstats-shaped response with usage keyed relative to "now"."""
    payload = {
        "usage": [
            {"timestamp": nz_timestamp(days_ago=2, hour=10), "litres": 300},
            {"timestamp": nz_timestamp(days_ago=1, hour=8), "litres": 210},
            {"timestamp": nz_timestamp(days_ago=1, hour=20), "litres": 190},
        ],
        "statistics": {
            "currentPeriodAverage": 98,
            "differenceToPreviousPeriod": -8,
            "efficiency": {"currentHouseholdBand": 1, "usageToLowerBand": 0},
        },
        "accountBalance": -12.3,
        "amountDue": 0,
        "readingType": "Actual",
    }
    return json.dumps(payload)


async def test_process_daily_data_state_is_yesterdays_consumption(
    make_sensor: Callable[..., WatercareUsageSensor], relative_response: str
) -> None:
    sensor = make_sensor(endpoint="dailywithstats")

    await sensor.process_daily_data(relative_response)

    assert sensor.state == 400  # yesterday's two readings: 210 + 190
    assert sensor.extra_state_attributes["yesterday_consumption"] == 400
    assert sensor.extra_state_attributes["currentHouseholdBand"] == 1


async def test_process_daily_data_generates_running_sum_statistics(
    make_sensor: Callable[..., WatercareUsageSensor],
    relative_response: str,
    patch_recorder: SimpleNamespace,
) -> None:
    sensor = make_sensor(endpoint="dailywithstats")

    await sensor.process_daily_data(relative_response)

    consumption_points = patch_recorder.written["watercare:daily_consumption"]
    assert [p["sum"] for p in consumption_points] == [300, 700]
    assert "watercare:daily_cost" in patch_recorder.written


async def test_process_daily_data_smoke_test_static_fixture(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    """Static fixture (fixed historical dates) still parses without raising."""
    sensor = make_sensor(endpoint="dailywithstats")

    await sensor.process_daily_data(load_fixture("dailywithstats_response.json"))

    assert sensor.extra_state_attributes["endpoint"] == "dailywithstats"


async def test_process_daily_data_handles_none_response(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor(endpoint="dailywithstats")

    await sensor.process_daily_data(None)

    assert sensor.state is None


async def test_process_daily_data_handles_malformed_json(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor(endpoint="dailywithstats")
    sensor._state = 123.45  # seed a sentinel to prove it's left untouched

    await sensor.process_daily_data("not json")

    assert sensor.state == 123.45
