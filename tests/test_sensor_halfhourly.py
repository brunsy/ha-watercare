"""Tests for the halfhourly endpoint bucketing, watermark push, and state logic."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from custom_components.watercare import sensor as watercare_sensor
from custom_components.watercare.const import NZ_TIMEZONE
from tests.conftest import (
    load_fixture,
    nz_full_day_readings,
    nz_timestamp,
    nz_timestamp_on,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import SimpleNamespace

    from custom_components.watercare.sensor import WatercareUsageSensor


@pytest.fixture
def readings() -> list[dict]:
    return json.loads(load_fixture("halfhourly_readings.json"))


def test_heal_lookback_exceeds_the_regular_poll_window() -> None:
    """
    Regression: the healing anchor lookback must stay ahead of the poll window.

    _HEAL_LOOKBACK_STATS is derived from HALFHOURLY_POLL_WINDOW_DAYS rather
    than a bare number specifically so the two can't drift apart -- if the
    poll window ever grew to meet or exceed the lookback, the anchor query
    in _async_statistics_window could miss and silently fall back to a 0.0
    anchor (looks like a meter reset). This pins the derivation itself, not
    just today's values, so it fails if a future edit reintroduces a gap.
    """
    assert watercare_sensor._HEAL_LOOKBACK_STATS > (
        24 * watercare_sensor.HALFHOURLY_POLL_WINDOW_DAYS
    )


def test_bucket_hourly_readings_sums_half_hours_into_hours(
    make_sensor: Callable[..., WatercareUsageSensor], readings: list[dict]
) -> None:
    sensor = make_sensor(endpoint="halfhourly")

    buckets, latest_complete_date = sensor._bucket_hourly_readings(readings)

    assert len(buckets) == 3
    sums_in_order = [buckets[hour] for hour in sorted(buckets)]
    assert sums_in_order == [35, 40, 5]  # (20+15), (30+10), (5)
    assert latest_complete_date is None  # fixture covers only 12:00-14:00 NZT


def test_bucket_hourly_readings_finds_latest_complete_date_in_same_pass(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor(endpoint="halfhourly")
    nz_now = datetime.now(NZ_TIMEZONE)
    yesterday = (nz_now - timedelta(days=1)).date()
    readings = nz_full_day_readings(yesterday, {(23, 0): 5, (23, 30): 5})

    buckets, latest_complete_date = sensor._bucket_hourly_readings(readings)

    assert buckets[max(buckets)] == 10  # 23:00 and 23:30 share an hourly bucket
    assert latest_complete_date == yesterday


def test_bucket_hourly_readings_sparse_day_with_last_slot_not_complete(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    """
    Regression: a sparse mid-publish response must not mark its day complete.

    Seen live at 07:15 NZDT (18:15 UTC) on 2026-10-14: the response had
    only a handful of the previous day's slots, 23:30 among them. Under the
    old "23:30 seen" rule the day counted as complete, _is_data_fresh
    stopped the retry ladder, and the day's statistics stayed mostly empty.
    """
    sensor = make_sensor(endpoint="halfhourly")
    nz_now = datetime.now(NZ_TIMEZONE)
    yesterday = (nz_now - timedelta(days=1)).date()
    readings = [
        r
        for r in nz_full_day_readings(yesterday)
        if r["timestamp"]
        in {
            nz_timestamp_on(yesterday, 0, 0),
            nz_timestamp_on(yesterday, 4, 0),
            nz_timestamp_on(yesterday, 21, 0),
            nz_timestamp_on(yesterday, 23, 0),
            nz_timestamp_on(yesterday, 23, 30),
        }
    ]

    _, latest_complete_date = sensor._bucket_hourly_readings(readings)

    assert latest_complete_date is None


@pytest.mark.parametrize(
    ("dst_day", "slots"),
    [(date(2026, 9, 27), 46), (date(2026, 4, 5), 50)],
    ids=["dst-start", "dst-end"],
)
def test_bucket_hourly_readings_dst_day_complete_with_all_its_slots(
    make_sensor: Callable[..., WatercareUsageSensor], dst_day: date, slots: int
) -> None:
    """A DST day is complete with its own 46/50 slots, and not one short."""
    sensor = make_sensor(endpoint="halfhourly")
    readings = nz_full_day_readings(dst_day)
    assert len(readings) == slots

    _, latest_complete_date = sensor._bucket_hourly_readings(readings)
    assert latest_complete_date == dst_day

    # Drop one slot from the middle of the day -- on the DST-end day, one
    # of the two 02:30s, which only differ by UTC instant.
    readings.pop(5)
    _, latest_complete_date = sensor._bucket_hourly_readings(readings)
    assert latest_complete_date is None


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


async def test_push_hourly_statistic_heal_since_backfills_gap(
    make_sensor: Callable[..., WatercareUsageSensor],
    patch_recorder: SimpleNamespace,
) -> None:
    """
    Regression: heal a gap a sparser earlier poll's response skipped.

    A fuller poll must correct any already-stored later hour too.
    Reproduces the live bug: Watercare's API returned a sparse set of
    half-hourly readings for a day that still included the 23:30 reading,
    so the day was (correctly) marked complete and pushed -- but with a
    gap at hour 12:00 the response was missing. The append-only watermark
    then permanently blocked hour 12:00 from ever being written, even once
    a later poll's response included it, because hour 14:00 (after the
    gap) had already advanced the watermark past it.
    """
    sensor = make_sensor(endpoint="halfhourly")
    nz_now = datetime.now(NZ_TIMEZONE)
    hour_12 = nz_now.replace(hour=12, minute=0, second=0, microsecond=0)
    hour_14 = hour_12 + timedelta(hours=2)
    # Simulates the earlier sparse poll: it only ever saw hour 14:00.
    patch_recorder.stored_statistics["watercare:halfhourly_consumption"] = [
        (4.0, hour_14.timestamp()),
    ]
    # This poll's fuller response fills the gap at hour 12:00.
    buckets = {hour_12: 23, hour_14: 4}

    new_count = await sensor._async_push_hourly_statistic(
        buckets,
        statistic_id="watercare:halfhourly_consumption",
        name="Watercare Half-hourly Consumption",
        unit="L",
        cost_key=None,
        rate_gate=None,
        heal_since=hour_12,
    )

    assert new_count == 2
    points = {
        p["start"]: p["sum"]
        for p in patch_recorder.written["watercare:halfhourly_consumption"]
    }
    assert points[hour_12] == 23
    assert points[hour_14] == 27  # healed: 23 + 4, not the stale 4


async def test_push_hourly_statistic_heal_since_preserves_hours_missing_from_this_poll(
    make_sensor: Callable[..., WatercareUsageSensor],
    patch_recorder: SimpleNamespace,
) -> None:
    """
    Regression: healing must leave an hour missing from this poll alone.

    Its stored sum must still be carried forward as a checkpoint for later
    hours' running totals rather than being dropped.
    """
    sensor = make_sensor(endpoint="halfhourly")
    nz_now = datetime.now(NZ_TIMEZONE)
    hour_11 = nz_now.replace(hour=11, minute=0, second=0, microsecond=0)
    hour_12 = hour_11 + timedelta(hours=1)
    hour_13 = hour_11 + timedelta(hours=2)
    patch_recorder.stored_statistics["watercare:halfhourly_consumption"] = [
        (10.0, hour_11.timestamp()),  # anchor: before the healing window
        (999.0, hour_12.timestamp()),  # already stored, absent from this poll
    ]
    buckets = {hour_13: 5}  # this poll only returned hour 13:00

    new_count = await sensor._async_push_hourly_statistic(
        buckets,
        statistic_id="watercare:halfhourly_consumption",
        name="Watercare Half-hourly Consumption",
        unit="L",
        cost_key=None,
        rate_gate=None,
        heal_since=hour_12,
    )

    assert new_count == 1  # hour_12 untouched, not re-sent
    points = patch_recorder.written["watercare:halfhourly_consumption"]
    assert points[0]["start"] == hour_13
    assert points[0]["sum"] == 1004  # carried forward from hour_12's stored 999, + 5


async def test_push_hourly_statistic_heal_since_ignores_hours_before_window(
    make_sensor: Callable[..., WatercareUsageSensor],
    patch_recorder: SimpleNamespace,
) -> None:
    """
    Regression: an hourly_consumption entry before heal_since is ignored.

    anchor_sum already accounts for everything up to just before
    heal_since, so folding an earlier hour into the running sum too would
    double-count it.
    """
    sensor = make_sensor(endpoint="halfhourly")
    nz_now = datetime.now(NZ_TIMEZONE)
    hour_11 = nz_now.replace(hour=11, minute=0, second=0, microsecond=0)
    hour_12 = hour_11 + timedelta(hours=1)
    patch_recorder.stored_statistics["watercare:halfhourly_consumption"] = [
        (10.0, hour_11.timestamp()),  # anchor already includes hour_11's litres
    ]
    # This poll's response still (redundantly) includes hour_11 alongside
    # the new hour_12 -- hour_11 must not be re-added on top of the anchor.
    buckets = {hour_11: 10, hour_12: 5}

    new_count = await sensor._async_push_hourly_statistic(
        buckets,
        statistic_id="watercare:halfhourly_consumption",
        name="Watercare Half-hourly Consumption",
        unit="L",
        cost_key=None,
        rate_gate=None,
        heal_since=hour_12,
    )

    assert new_count == 1  # hour_11 excluded, only hour_12 written
    points = patch_recorder.written["watercare:halfhourly_consumption"]
    assert points[0]["start"] == hour_12
    assert points[0]["sum"] == 15  # 10 (anchor) + 5, not 10 + 10 + 5


async def test_push_hourly_statistic_heal_since_skips_unchanged_recomputed_hours(
    make_sensor: Callable[..., WatercareUsageSensor],
    patch_recorder: SimpleNamespace,
) -> None:
    """
    Regression: healing must not re-upsert an unchanged recomputed hour.

    Healing re-touches the whole window every poll, so without this,
    every already-correct hour in the window would get rewritten with an
    unchanged value on every single poll.
    """
    sensor = make_sensor(endpoint="halfhourly")
    nz_now = datetime.now(NZ_TIMEZONE)
    hour_12 = nz_now.replace(hour=12, minute=0, second=0, microsecond=0)
    hour_13 = hour_12 + timedelta(hours=1)
    patch_recorder.stored_statistics["watercare:halfhourly_consumption"] = [
        (23.0, hour_12.timestamp()),
    ]
    # This poll re-fetches hour_12 (unchanged) and adds new hour_13.
    buckets = {hour_12: 23, hour_13: 5}

    new_count = await sensor._async_push_hourly_statistic(
        buckets,
        statistic_id="watercare:halfhourly_consumption",
        name="Watercare Half-hourly Consumption",
        unit="L",
        cost_key=None,
        rate_gate=None,
        heal_since=hour_12,
    )

    assert new_count == 1  # hour_12 skipped, only the genuinely new hour_13
    points = patch_recorder.written["watercare:halfhourly_consumption"]
    assert points[0]["start"] == hour_13
    assert points[0]["sum"] == 28


async def test_push_hourly_statistic_heal_since_unchanged_skip_tolerates_float_roundoff(
    make_sensor: Callable[..., WatercareUsageSensor],
    patch_recorder: SimpleNamespace,
) -> None:
    """
    Regression: the unchanged-hour skip must use a float tolerance.

    A cost series recomputation can land a rounding ULP off the originally
    stored value even when nothing actually changed (division/multiplication
    aren't exact in floating point) -- exact equality would treat that as a
    real change and re-upsert it every poll, defeating the optimization for
    exactly the series it matters most for.
    """
    sensor = make_sensor(endpoint="halfhourly")
    nz_now = datetime.now(NZ_TIMEZONE)
    hour_12 = nz_now.replace(hour=12, minute=0, second=0, microsecond=0)
    stored_sum = 23.000000000000004  # a plausible one-ULP-off stored value
    patch_recorder.stored_statistics["watercare:halfhourly_consumption"] = [
        (stored_sum, hour_12.timestamp()),
    ]
    buckets = {hour_12: 23}  # recomputes to exactly 23.0, not stored_sum

    new_count = await sensor._async_push_hourly_statistic(
        buckets,
        statistic_id="watercare:halfhourly_consumption",
        name="Watercare Half-hourly Consumption",
        unit="L",
        cost_key=None,
        rate_gate=None,
        heal_since=hour_12,
    )

    assert new_count == 0
    assert "watercare:halfhourly_consumption" not in patch_recorder.written


async def test_push_hourly_statistic_heal_since_shifts_untouched_later_hour(
    make_sensor: Callable[..., WatercareUsageSensor],
    patch_recorder: SimpleNamespace,
) -> None:
    """
    Regression: correcting an earlier hour must not leave a later hour stale.

    Reproduces a non-monotonic sum sequence: hour_h2 gets a big correction
    this poll, but hour_h3 (already stored, sparse-response gap from a
    previous poll) isn't in this poll's response at all. Leaving hour_h3's
    stored sum untouched would make it chronologically *smaller* than the
    now-corrected hour_h2 -- a "cumulative" total that decreases, which
    e.g. the Energy dashboard would render as a large negative usage spike.
    """
    sensor = make_sensor(endpoint="halfhourly")
    nz_now = datetime.now(NZ_TIMEZONE)
    hour_h2 = nz_now.replace(hour=12, minute=0, second=0, microsecond=0)
    hour_h3 = hour_h2 + timedelta(hours=1)
    patch_recorder.stored_statistics["watercare:halfhourly_consumption"] = [
        (6.0, hour_h2.timestamp()),  # understated by a prior sparse poll
        (9.0, hour_h3.timestamp()),  # correct on its own, but not refetched
    ]
    # This poll only returns hour_h2, with the true (much larger) reading.
    buckets = {hour_h2: 1000}

    new_count = await sensor._async_push_hourly_statistic(
        buckets,
        statistic_id="watercare:halfhourly_consumption",
        name="Watercare Half-hourly Consumption",
        unit="L",
        cost_key=None,
        rate_gate=None,
        heal_since=hour_h2,
    )

    assert new_count == 2  # both hours corrected, even though h3 wasn't refetched
    points = {
        p["start"]: p["sum"]
        for p in patch_recorder.written["watercare:halfhourly_consumption"]
    }
    assert points[hour_h2] == 1000
    # h3 shifts by the same +994 correction, preserving its own +3 delta
    # over h2 (9 - 6 = 3) instead of staying at its stale absolute value.
    assert points[hour_h3] == 1003
    assert points[hour_h3] > points[hour_h2]  # monotonic


async def test_heal_hourly_points_carries_zero_delta_hole_unchanged(
    make_sensor: Callable[..., WatercareUsageSensor],
    patch_recorder: SimpleNamespace,
) -> None:
    """A hole with no preceding correction this poll is still left untouched."""
    sensor = make_sensor(endpoint="halfhourly")
    nz_now = datetime.now(NZ_TIMEZONE)
    hour_h2 = nz_now.replace(hour=12, minute=0, second=0, microsecond=0)
    hour_h3 = hour_h2 + timedelta(hours=1)
    patch_recorder.stored_statistics["watercare:halfhourly_consumption"] = [
        (6.0, hour_h2.timestamp()),
        (9.0, hour_h3.timestamp()),
    ]
    buckets = {hour_h2: 6}  # matches what's already stored -- no correction

    new_count = await sensor._async_push_hourly_statistic(
        buckets,
        statistic_id="watercare:halfhourly_consumption",
        name="Watercare Half-hourly Consumption",
        unit="L",
        cost_key=None,
        rate_gate=None,
        heal_since=hour_h2,
    )

    assert new_count == 0
    assert "watercare:halfhourly_consumption" not in patch_recorder.written


async def test_heal_hourly_points_ignores_rate_change_outside_fresh_hours(
    make_sensor: Callable[..., WatercareUsageSensor],
    patch_recorder: SimpleNamespace,
) -> None:
    """
    Regression: a rate change alone must not rewrite an untouched hour's cost.

    Cost is a pure function of litres and the *current* rate, so
    recomputing it for every hour in the window unconditionally would
    silently rewrite already-published cost history the moment the user
    changes a rate -- even for hours whose litres never changed. Passing
    an explicit `fresh_hours` that excludes an hour (as
    _async_push_halfhourly_statistics does using the consumption series'
    own touched-hour set) must leave that hour's cost untouched.
    """
    sensor = make_sensor(endpoint="halfhourly", consumption_rate=5.0)
    nz_now = datetime.now(NZ_TIMEZONE)
    hour_h = nz_now.replace(hour=12, minute=0, second=0, microsecond=0)
    # Stored cost reflects what an earlier, different rate would have
    # produced for this same litres value (100L @ rate 2.0 -> 0.2).
    patch_recorder.stored_statistics["watercare:halfhourly_consumption_cost"] = [
        (0.2, hour_h.timestamp()),
    ]
    buckets = {hour_h: 100}  # same litres as before, just refetched this poll

    points = await sensor._async_heal_hourly_points(
        buckets,
        statistic_id="watercare:halfhourly_consumption_cost",
        heal_since=hour_h,
        cost_key="consumption",
        fresh_hours=set(),  # consumption series decided this hour didn't change
    )

    assert points == []  # not recomputed with the new rate (100/1000*5.0=0.5)


async def test_push_halfhourly_statistics_rate_change_does_not_touch_stable_hour(
    make_sensor: Callable[..., WatercareUsageSensor],
    patch_recorder: SimpleNamespace,
) -> None:
    """Same as above, exercised through the real four-series wiring."""
    sensor = make_sensor(endpoint="halfhourly", consumption_rate=5.0)
    nz_now = datetime.now(NZ_TIMEZONE)
    hour_h = nz_now.replace(hour=12, minute=0, second=0, microsecond=0)
    # Consumption is unchanged (recomputes to exactly what's stored), so
    # the consumption series won't touch hour_h -- but its cost was stored
    # under a different (now-changed) rate.
    patch_recorder.stored_statistics["watercare:halfhourly_consumption"] = [
        (100.0, hour_h.timestamp()),
    ]
    patch_recorder.stored_statistics["watercare:halfhourly_consumption_cost"] = [
        (0.2, hour_h.timestamp()),  # 100L @ the old rate of 2.0
    ]
    buckets = {hour_h: 100}

    new_count = await sensor._async_push_halfhourly_statistics(
        buckets, heal_since=hour_h
    )

    assert new_count == 0  # consumption itself didn't change
    assert "watercare:halfhourly_consumption_cost" not in patch_recorder.written


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


async def test_process_halfhourly_data_marks_day_complete_once_every_slot_seen(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor(endpoint="halfhourly")
    nz_now = datetime.now(NZ_TIMEZONE)
    yesterday = (nz_now - timedelta(days=1)).date()
    payload = nz_full_day_readings(yesterday, {(23, 30): 20})

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
    payload = nz_full_day_readings(older_complete, {(23, 30): 20})

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
        *nz_full_day_readings(complete_day, {(23, 30): 10}),
        {"timestamp": nz_timestamp_on(partial_day, 23, 0), "litres": 20},
    ]

    await sensor.process_halfhourly_data(json.dumps(payload))

    points = patch_recorder.written["watercare:halfhourly_consumption"]
    # Only the complete day's buckets.
    assert all(p["start"].date() == complete_day for p in points)
    assert points[-1]["sum"] == 10
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
    payload = nz_full_day_readings(yesterday, {(23, 30): 20})

    with pytest.raises(RuntimeError):
        await sensor.process_halfhourly_data(json.dumps(payload))

    assert sensor._latest_reading_date is None
