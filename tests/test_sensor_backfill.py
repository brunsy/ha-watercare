"""Tests for async_backfill_halfhourly_history."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

from custom_components.watercare.const import NZ_TIMEZONE
from tests.conftest import load_fixture, nz_timestamp_on

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import SimpleNamespace

    import pytest

    from custom_components.watercare.sensor import WatercareUsageSensor

CHUNK_RESPONSE = load_fixture("halfhourly_readings.json")
# The fixture alone has no 23:30 NZT reading, so no day in it counts as
# complete and the backfill defers its push. Write-path tests append one
# (2026-06-15T11:30Z = 23:30 NZST on the fixture's own day) so the whole
# chunk is a confirmed-complete day.
COMPLETE_CHUNK_RESPONSE = json.dumps(
    [
        *json.loads(CHUNK_RESPONSE),
        {"timestamp": "2026-06-15T11:30:00.000000Z", "litres": 20},
    ]
)


async def test_backfill_pages_until_empty_response_then_writes(
    make_sensor: Callable[..., WatercareUsageSensor], patch_recorder: SimpleNamespace
) -> None:
    sensor = make_sensor(endpoint="halfhourly")
    sensor._api.get_data = AsyncMock(side_effect=[COMPLETE_CHUNK_RESPONSE, "[]"])

    await sensor.async_backfill_halfhourly_history()

    assert sensor._api.get_data.await_count == 2
    consumption_points = patch_recorder.written["watercare:halfhourly_consumption"]
    assert [p["sum"] for p in consumption_points] == [35, 75, 80, 100]


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
    sensor._api.get_data = AsyncMock(side_effect=[COMPLETE_CHUNK_RESPONSE, "[]"])

    readings = json.loads(COMPLETE_CHUNK_RESPONSE)
    buckets, _ = sensor._bucket_hourly_readings(readings)
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


async def test_backfill_defers_push_when_no_complete_day_observed(
    make_sensor: Callable[..., WatercareUsageSensor],
    patch_recorder: SimpleNamespace,
) -> None:
    """
    Regression: history with no confirmed-complete day must not be pushed.

    A brand-new meter whose only history is a few hours of today has no
    23:30 NZT reading anywhere; pushing those buckets would lock partial
    sums in via the append-only watermark. Regular polling pushes once a
    complete day exists.
    """
    sensor = make_sensor(endpoint="halfhourly")
    sensor._api.get_data = AsyncMock(side_effect=[CHUNK_RESPONSE, "[]"])

    await sensor.async_backfill_halfhourly_history()

    assert patch_recorder.written == {}
    assert sensor._latest_reading_date is None


async def test_backfill_seeds_watermark_and_defers_partial_trailing_day(
    make_sensor: Callable[..., WatercareUsageSensor],
    patch_recorder: SimpleNamespace,
) -> None:
    """
    Regression: a backfill run mid-publish must not lock in a partial hour.

    Same guard as process_halfhourly_data: only confirmed-complete days
    are pushed. The observed complete date also seeds the freshness
    watermark, so a failed post-backfill regular update doesn't leave the
    sensor thinking it has never seen a complete day.
    """
    sensor = make_sensor(endpoint="halfhourly")
    nz_now = datetime.now(NZ_TIMEZONE)
    complete_day = (nz_now - timedelta(days=2)).date()
    partial_day = (nz_now - timedelta(days=1)).date()
    chunk = json.dumps(
        [
            {"timestamp": nz_timestamp_on(complete_day, 23, 30), "litres": 10},
            {"timestamp": nz_timestamp_on(partial_day, 23, 0), "litres": 20},
        ]
    )
    sensor._api.get_data = AsyncMock(side_effect=[chunk, "[]"])

    await sensor.async_backfill_halfhourly_history()

    points = patch_recorder.written["watercare:halfhourly_consumption"]
    assert len(points) == 1  # only the complete day's bucket
    assert points[0]["sum"] == 10
    assert sensor._latest_reading_date == complete_day


async def test_update_is_serialized_behind_backfill(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    """
    Regression: a poll landing mid-backfill must wait, not race.

    The watercare.backfill_history service can run while the scheduled
    tick is active; an unserialized poll would push current-window buckets
    and advance the append-only watermark past the backfill's older
    history, silently discarding it.
    """
    sensor = make_sensor(endpoint="halfhourly")
    release = asyncio.Event()
    calls: list[str] = []

    async def fake_get_data(**_kwargs: Any) -> str:
        calls.append("fetch")
        if len(calls) == 1:
            await release.wait()
        return "[]"

    sensor._api.get_data = AsyncMock(side_effect=fake_get_data)

    backfill_task = asyncio.create_task(sensor.async_backfill_halfhourly_history())
    for _ in range(5):
        await asyncio.sleep(0)
    update_task = asyncio.create_task(sensor.async_update())
    for _ in range(5):
        await asyncio.sleep(0)

    assert calls == ["fetch"]  # the update is blocked behind the backfill's lock
    release.set()
    await backfill_task
    await update_task
    assert calls == ["fetch", "fetch"]
