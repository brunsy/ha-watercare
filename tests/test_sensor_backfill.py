"""Tests for async_backfill_halfhourly_history."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from tests.conftest import load_fixture

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import SimpleNamespace

    import pytest

    from custom_components.watercare.sensor import WatercareUsageSensor

CHUNK_RESPONSE = load_fixture("halfhourly_readings.json")


async def test_backfill_pages_until_empty_response_then_writes(
    make_sensor: Callable[..., WatercareUsageSensor], patch_recorder: SimpleNamespace
) -> None:
    sensor = make_sensor(endpoint="halfhourly")
    sensor._api.get_data = AsyncMock(side_effect=[CHUNK_RESPONSE, "[]"])

    await sensor.async_backfill_halfhourly_history()

    assert sensor._api.get_data.await_count == 2
    consumption_points = patch_recorder.written["watercare:halfhourly_consumption"]
    assert [p["sum"] for p in consumption_points] == [35, 75, 80]


async def test_backfill_aborts_without_writing_on_failed_chunk(
    make_sensor: Callable[..., WatercareUsageSensor], patch_recorder: SimpleNamespace
) -> None:
    """Regression: a failed chunk must not write any partial statistics."""
    sensor = make_sensor(endpoint="halfhourly")
    sensor._api.get_data = AsyncMock(side_effect=[CHUNK_RESPONSE, None])

    await sensor.async_backfill_halfhourly_history()

    assert patch_recorder.written == {}


async def test_backfill_aborts_without_writing_on_malformed_chunk(
    make_sensor: Callable[..., WatercareUsageSensor], patch_recorder: SimpleNamespace
) -> None:
    sensor = make_sensor(endpoint="halfhourly")
    sensor._api.get_data = AsyncMock(side_effect=["not json"])

    await sensor.async_backfill_halfhourly_history()

    assert patch_recorder.written == {}


async def test_backfill_warns_when_existing_watermark_blocks_older_data(
    make_sensor: Callable[..., WatercareUsageSensor],
    patch_recorder: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Regression: re-running backfill after the watermark has advanced should warn.

    It must not silently no-op.
    """
    sensor = make_sensor(endpoint="halfhourly")
    sensor._api.get_data = AsyncMock(side_effect=[CHUNK_RESPONSE, "[]"])

    readings = json.loads(CHUNK_RESPONSE)
    buckets = sensor._bucket_hourly_readings(readings)
    newest_bucket = max(buckets)
    # All four series already have a watermark newer than everything backfill
    # just collected -- as if regular polling already ran to completion.
    for statistic_id in (
        "watercare:halfhourly_consumption",
        "watercare:halfhourly_cost",
        "watercare:halfhourly_consumption_cost",
        "watercare:halfhourly_wastewater_cost",
    ):
        patch_recorder.last_statistics[statistic_id] = (
            999.0,
            newest_bucket.timestamp(),
        )

    with caplog.at_level(logging.WARNING):
        await sensor.async_backfill_halfhourly_history()

    assert patch_recorder.written == {}
    assert any("won't be written" in message for message in caplog.messages)
