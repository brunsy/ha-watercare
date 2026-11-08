"""Tests for process_data/generate_statistics (mechanicalmonthly billing periods)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.conftest import load_fixture

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import SimpleNamespace

    from custom_components.watercare.sensor import WatercareUsageSensor


@pytest.fixture
def response() -> str:
    return load_fixture("mechanicalmonthly_billing_periods.json")


async def test_process_data_sets_state_from_first_list_entry(
    make_sensor: Callable[..., WatercareUsageSensor], response: str
) -> None:
    """process_data trusts billing_periods[0] as "latest" -- no re-sort by date."""
    sensor = make_sensor(endpoint="mechanicalmonthly")

    await sensor.process_data(response)

    assert sensor.state == 15000  # June period, first in the fixture list
    assert sensor.extra_state_attributes["billing_period_usage"] == 15000
    assert (
        sensor.extra_state_attributes["billing_period_to"] == "2026-06-30T00:00:00.000Z"
    )
    assert sensor.extra_state_attributes["endpoint"] == "mechanicalmonthly"


async def test_generate_statistics_running_sum_across_periods(
    make_sensor: Callable[..., WatercareUsageSensor],
    response: str,
    patch_recorder: SimpleNamespace,
) -> None:
    sensor = make_sensor(endpoint="mechanicalmonthly")

    await sensor.process_data(response)

    consumption_points = patch_recorder.written["watercare:water_consumption"]
    # generate_statistics sorts ascending by billingPeriodToDate regardless
    # of input order: May (18000) then June (15000).
    assert [p["sum"] for p in consumption_points] == [18000, 33000]

    cost_points = patch_recorder.written["watercare:water_cost"]
    assert len(cost_points) == 2
    assert cost_points[-1]["sum"] > cost_points[0]["sum"]

    consumption_cost_points = patch_recorder.written["watercare:consumption_cost"]
    wastewater_cost_points = patch_recorder.written["watercare:wastewater_cost"]
    assert len(consumption_cost_points) == 2
    assert len(wastewater_cost_points) == 2


async def test_process_data_handles_none_response(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor(endpoint="mechanicalmonthly")

    await sensor.process_data(None)

    assert sensor.state is None


async def test_process_data_handles_malformed_json(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor(endpoint="mechanicalmonthly")
    sensor._state = 123.45  # seed a sentinel to prove it's left untouched

    await sensor.process_data("not json")

    assert sensor.state == 123.45
