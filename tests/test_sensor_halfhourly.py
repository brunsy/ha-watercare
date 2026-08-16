"""Tests for the halfhourly endpoint bucketing, watermark push, and state logic."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from custom_components.watercare.const import NZ_TIMEZONE
from tests.conftest import load_fixture, nz_timestamp, nz_timestamp_on

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import SimpleNamespace

    from custom_components.watercare.sensor import WatercareUsageSensor


@pytest.fixture
def readings() -> list[dict]:
    return json.loads(load_fixture("halfhourly_readings.json"))


def test_bucket_hourly_readings_sums_half_hours_into_hours(
    make_sensor: Callable[..., WatercareUsageSensor], readings: list[dict]
) -> None:
    sensor = make_sensor(endpoint="halfhourly")

    buckets, latest_complete_date = sensor._bucket_hourly_readings(readings)

    assert len(buckets) == 3
    sums_in_order = [buckets[hour] for hour in sorted(buckets)]
    assert sums_in_order == [35, 40, 5]  # (20+15), (30+10), (5)
    assert latest_complete_date is None  # fixture has no 23:30 NZT reading


def test_bucket_hourly_readings_finds_latest_complete_date_in_same_pass(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor(endpoint="halfhourly")
    nz_now = datetime.now(NZ_TIMEZONE)
    yesterday = (nz_now - timedelta(days=1)).date()
    readings = [
        {"timestamp": nz_timestamp_on(yesterday, 23, 0), "litres": 5},
        {"timestamp": nz_timestamp_on(yesterday, 23, 30), "litres": 5},
    ]

    buckets, latest_complete_date = sensor._bucket_hourly_readings(readings)

    assert len(buckets) == 1  # both fall in the same hourly bucket
    assert latest_complete_date == yesterday


async def test_push_hourly_statistic_no_existing_watermark_writes_all(
    make_sensor: Callable[..., WatercareUsageSensor],
    readings: list[dict],
    patch_recorder: SimpleNamespace,
) -> None:
    sensor = make_sensor(endpoint="halfhourly")
    buckets, _ = sensor._bucket_hourly_readings(readings)

    new_count = await sensor._async_push_hourly_statistic(
        buckets,
        statistic_id="watercare:halfhourly_consumption",
        name="Watercare Half-hourly Consumption",
        unit="L",
        cost_key=None,
        rate_gate=None,
    )

    assert new_count == 3
    points = patch_recorder.written["watercare:halfhourly_consumption"]
    assert [p["sum"] for p in points] == [35, 75, 80]


async def test_push_hourly_statistic_continues_from_existing_watermark(
    make_sensor: Callable[..., WatercareUsageSensor],
    readings: list[dict],
    patch_recorder: SimpleNamespace,
) -> None:
    """Regression: must skip buckets at/before the watermark, continuing the sum."""
    sensor = make_sensor(endpoint="halfhourly")
    buckets, _ = sensor._bucket_hourly_readings(readings)
    first_bucket_start = min(buckets)
    patch_recorder.last_statistics["watercare:halfhourly_consumption"] = (
        1000.0,
        first_bucket_start.timestamp(),
    )

    new_count = await sensor._async_push_hourly_statistic(
        buckets,
        statistic_id="watercare:halfhourly_consumption",
        name="Watercare Half-hourly Consumption",
        unit="L",
        cost_key=None,
        rate_gate=None,
    )

    assert new_count == 2  # first bucket skipped, two remain
    points = patch_recorder.written["watercare:halfhourly_consumption"]
    assert [p["sum"] for p in points] == [1040, 1045]


async def test_push_hourly_statistic_nothing_new_writes_nothing(
    make_sensor: Callable[..., WatercareUsageSensor],
    readings: list[dict],
    patch_recorder: SimpleNamespace,
) -> None:
    """Regression: must not call async_add_external_statistics with nothing new."""
    sensor = make_sensor(endpoint="halfhourly")
    buckets, _ = sensor._bucket_hourly_readings(readings)
    last_bucket_start = max(buckets)
    patch_recorder.last_statistics["watercare:halfhourly_consumption"] = (
        500.0,
        last_bucket_start.timestamp(),
    )

    new_count = await sensor._async_push_hourly_statistic(
        buckets,
        statistic_id="watercare:halfhourly_consumption",
        name="Watercare Half-hourly Consumption",
        unit="L",
        cost_key=None,
        rate_gate=None,
    )

    assert new_count == 0
    assert "watercare:halfhourly_consumption" not in patch_recorder.written


async def test_push_hourly_statistic_zero_rate_gate_skips_entirely(
    make_sensor: Callable[..., WatercareUsageSensor],
    readings: list[dict],
    patch_recorder: SimpleNamespace,
) -> None:
    sensor = make_sensor(endpoint="halfhourly")
    buckets, _ = sensor._bucket_hourly_readings(readings)

    new_count = await sensor._async_push_hourly_statistic(
        buckets,
        statistic_id="watercare:halfhourly_consumption_cost",
        name="Watercare Half-hourly Consumption Cost",
        unit="NZD",
        cost_key="consumption",
        rate_gate=0,
    )

    assert new_count == 0
    assert "watercare:halfhourly_consumption_cost" not in patch_recorder.written


async def test_push_halfhourly_statistics_writes_all_four_series(
    make_sensor: Callable[..., WatercareUsageSensor],
    readings: list[dict],
    patch_recorder: SimpleNamespace,
) -> None:
    sensor = make_sensor(endpoint="halfhourly")
    buckets, _ = sensor._bucket_hourly_readings(readings)

    new_count = await sensor._async_push_halfhourly_statistics(buckets)

    assert new_count == 3
    assert set(patch_recorder.written) == {
        "watercare:halfhourly_consumption",
        "watercare:halfhourly_cost",
        "watercare:halfhourly_consumption_cost",
        "watercare:halfhourly_wastewater_cost",
    }


async def test_process_halfhourly_data_state_is_todays_consumption_so_far(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor(endpoint="halfhourly")
    payload = [
        {
            "timestamp": nz_timestamp(days_ago=1, hour=10),
            "litres": 999,
        },  # yesterday, excluded
        {"timestamp": nz_timestamp(days_ago=0, hour=9), "litres": 40},
        {"timestamp": nz_timestamp(days_ago=0, hour=15), "litres": 60},
    ]

    await sensor.process_halfhourly_data(json.dumps(payload))

    assert sensor.state == 100  # only today's two readings
    assert sensor.state_class == "measurement"
    assert sensor.extra_state_attributes["today_consumption"] == 100


async def test_process_halfhourly_data_handles_none_response(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor(endpoint="halfhourly")

    await sensor.process_halfhourly_data(None)

    assert sensor.state is None


async def test_process_halfhourly_data_handles_malformed_json(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor(endpoint="halfhourly")
    sensor._state = 123.45  # seed a sentinel to prove it's left untouched

    await sensor.process_halfhourly_data("not json")

    assert sensor.state == 123.45


async def test_process_halfhourly_data_partial_last_day_not_marked_complete(
    make_sensor: Callable[..., WatercareUsageSensor],
    patch_recorder: SimpleNamespace,
) -> None:
    """
    Regression: a lone 23:00 reading must not mark that day fresh.

    Watercare publishes half-hourly, so a day's last reading is 23:30 --
    an earlier 23:00-only reading (seen live: 23:00 landed 15 min before
    23:30) means the day isn't fully published yet, and _is_data_fresh
    must keep retrying rather than stopping early. With no complete day
    in the response at all, no statistics may be pushed either.
    """
    sensor = make_sensor(endpoint="halfhourly")
    payload = [{"timestamp": nz_timestamp(days_ago=0, hour=23, minute=0), "litres": 20}]

    await sensor.process_halfhourly_data(json.dumps(payload))

    assert sensor._latest_reading_date is None
    assert not patch_recorder.written


async def test_process_halfhourly_data_marks_day_complete_once_last_slot_seen(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor(endpoint="halfhourly")
    nz_now = datetime.now(NZ_TIMEZONE)
    yesterday = (nz_now - timedelta(days=1)).date()
    payload = [{"timestamp": nz_timestamp_on(yesterday, 23, 30), "litres": 20}]

    await sensor.process_halfhourly_data(json.dumps(payload))

    assert sensor._latest_reading_date == yesterday


async def test_process_halfhourly_data_does_not_regress_known_complete_date(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    """Regression: an older confirmed day must not reset a newer already-known one."""
    sensor = make_sensor(endpoint="halfhourly")
    nz_now = datetime.now(NZ_TIMEZONE)
    already_known = (nz_now - timedelta(days=1)).date()
    sensor._latest_reading_date = already_known
    older_complete = (nz_now - timedelta(days=3)).date()
    payload = [{"timestamp": nz_timestamp_on(older_complete, 23, 30), "litres": 20}]

    await sensor.process_halfhourly_data(json.dumps(payload))

    assert sensor._latest_reading_date == already_known


async def test_process_halfhourly_data_excludes_partial_trailing_day_from_push(
    make_sensor: Callable[..., WatercareUsageSensor],
    patch_recorder: SimpleNamespace,
) -> None:
    """
    Regression: a mid-publish poll must not write a partial trailing hour.

    Observed live at 06:14: yesterday's readings up to 23:00 but no 23:30
    yet. Pushing that bucket would advance the append-only watermark past
    it, permanently excluding the late 23:30 litres from the cumulative
    sums once the retry sees the complete day. The partial day is deferred;
    the entity state still counts every reading.
    """
    sensor = make_sensor(endpoint="halfhourly")
    nz_now = datetime.now(NZ_TIMEZONE)
    complete_day = (nz_now - timedelta(days=2)).date()
    partial_day = (nz_now - timedelta(days=1)).date()
    payload = [
        {"timestamp": nz_timestamp_on(complete_day, 23, 30), "litres": 10},
        {"timestamp": nz_timestamp_on(partial_day, 23, 0), "litres": 20},
    ]

    await sensor.process_halfhourly_data(json.dumps(payload))

    points = patch_recorder.written["watercare:halfhourly_consumption"]
    assert len(points) == 1  # only the complete day's bucket
    assert points[0]["sum"] == 10
    assert sensor._latest_reading_date == complete_day


async def test_process_halfhourly_data_failed_push_leaves_day_stale(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    """
    Regression: a failed statistics push must not mark the day fresh.

    Advancing the freshness watermark before the push succeeds would stop
    the retry ladder for the day with yesterday's statistics unwritten
    (e.g. recorder not ready at boot).
    """
    sensor = make_sensor(endpoint="halfhourly")
    sensor._async_push_halfhourly_statistics = AsyncMock(
        side_effect=RuntimeError("recorder not ready")
    )
    nz_now = datetime.now(NZ_TIMEZONE)
    yesterday = (nz_now - timedelta(days=1)).date()
    payload = [{"timestamp": nz_timestamp_on(yesterday, 23, 30), "litres": 20}]

    with pytest.raises(RuntimeError):
        await sensor.process_halfhourly_data(json.dumps(payload))

    assert sensor._latest_reading_date is None
