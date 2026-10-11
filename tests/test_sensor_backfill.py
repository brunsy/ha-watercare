"""Tests for async_backfill_halfhourly_history."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

from custom_components.watercare.const import NZ_TIMEZONE
from tests.conftest import load_fixture, nz_full_day_readings, nz_timestamp_on

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import SimpleNamespace

    import pytest

    from custom_components.watercare.sensor import WatercareUsageSensor

CHUNK_RESPONSE = load_fixture("halfhourly_readings.json")
# The fixture alone covers only 12:00-14:00 NZST on 2026-06-15, so no day
# in it counts as complete and the backfill defers its push. Write-path
# tests use the fixture's day with every other half-hour slot filled in
# (zero litres, plus 20 L at 23:30) so the whole chunk is a
# confirmed-complete day.
COMPLETE_CHUNK_RESPONSE = json.dumps(
    nz_full_day_readings(
        date(2026, 6, 15),
        {
            (12, 0): 20,
            (12, 30): 15,
            (13, 0): 30,
            (13, 30): 10,
            (14, 0): 5,
            (23, 30): 20,
        },
    )
)


async def test_backfill_pages_until_empty_response_then_writes(
    make_sensor: Callable[..., WatercareUsageSensor], patch_recorder: SimpleNamespace
) -> None:
    sensor = make_sensor(endpoint="halfhourly")
    sensor._api.get_data = AsyncMock(side_effect=[COMPLETE_CHUNK_RESPONSE, "[]"])

    await sensor.async_backfill_halfhourly_history()

    assert sensor._api.get_data.await_count == 2
    consumption_points = patch_recorder.written["watercare:halfhourly_consumption"]
    # 00:00-11:00 empty, then (20+15), (30+10), (5), flat to 22:00, 23:30's 20.
    assert [p["sum"] for p in consumption_points] == (
        [0] * 12 + [35, 75, 80] + [80] * 8 + [100]
    )


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


async def test_backfill_warns_against_this_accounts_own_statistics(
    make_sensor: Callable[..., WatercareUsageSensor],
    patch_recorder: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Regression: the "won't be written" check must use the account-scoped id.

    It used to check the account-less watercare:halfhourly_consumption,
    so an account added as a subentry was judged by the migrated account's
    statistics (or none at all) instead of its own.
    """
    sensor = make_sensor(endpoint="halfhourly")
    sensor._legacy_ids = False
    sensor._account_number = "0012345-02"
    sensor._api.get_data = AsyncMock(side_effect=[COMPLETE_CHUNK_RESPONSE, "[]"])

    readings = json.loads(COMPLETE_CHUNK_RESPONSE)
    buckets, _ = sensor._bucket_hourly_readings(readings)
    newest_bucket = max(buckets)
    for name in (
        "halfhourly_consumption",
        "halfhourly_cost",
        "halfhourly_consumption_cost",
        "halfhourly_wastewater_cost",
    ):
        patch_recorder.last_statistics[f"watercare:0012345_02_{name}"] = (
            999.0,
            newest_bucket.timestamp(),
        )

    with caplog.at_level(logging.WARNING):
        await sensor.async_backfill_halfhourly_history()

    assert patch_recorder.written == {}
    warning = next(m for m in caplog.messages if "won't be written" in m)
    assert "watercare:0012345_02_halfhourly_consumption" in warning


async def test_backfill_defers_push_when_no_complete_day_observed(
    make_sensor: Callable[..., WatercareUsageSensor],
    patch_recorder: SimpleNamespace,
) -> None:
    """
    Regression: history with no confirmed-complete day must not be pushed.

    A brand-new meter whose only history is a few hours of today has no
    fully published day anywhere; pushing those buckets would lock partial
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
            *nz_full_day_readings(complete_day, {(23, 30): 10}),
            {"timestamp": nz_timestamp_on(partial_day, 23, 0), "litres": 20},
        ]
    )
    sensor._api.get_data = AsyncMock(side_effect=[chunk, "[]"])

    await sensor.async_backfill_halfhourly_history()

    points = patch_recorder.written["watercare:halfhourly_consumption"]
    # Only the complete day's buckets.
    assert all(p["start"].date() == complete_day for p in points)
    assert points[-1]["sum"] == 10
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
