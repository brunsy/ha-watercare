"""Tests for the fixed UTC-time poll scheduling (should_poll=False path)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from datetime import date as date_type
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.watercare import sensor as watercare_sensor
from custom_components.watercare.const import NZ_TIMEZONE

if TYPE_CHECKING:
    from collections.abc import Callable

    from custom_components.watercare.sensor import WatercareUsageSensor


def nz_dt(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Build an NZ-time datetime."""
    return datetime(year, month, day, hour, minute, 0, tzinfo=NZ_TIMEZONE)


def utc_dt(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Build a UTC datetime (poll slots are matched in UTC)."""
    return datetime(year, month, day, hour, minute, 0, tzinfo=UTC)


def test_should_poll_is_disabled(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor()

    assert sensor.should_poll is False


def test_data_is_stale_before_any_complete_day_is_seen(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    """
    Regression: a halfhourly sensor with no observed data must count as stale.

    _latest_reading_date is in-memory only, so every HA restart re-enters
    this state; treating it as fresh would skip the retry ladder after a
    failed first update and leave the sensor empty until the next day's
    start hour.
    """
    sensor = make_sensor(endpoint="halfhourly")

    assert sensor._is_data_fresh(nz_dt(2026, 8, 16, 7)) is False


def test_data_is_fresh_once_previous_day_captured(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor(endpoint="halfhourly")
    now_nz = nz_dt(2026, 8, 16, 7)
    sensor._latest_reading_date = now_nz.date() - timedelta(days=1)

    assert sensor._is_data_fresh(now_nz) is True


def test_data_is_stale_when_days_behind(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor(endpoint="halfhourly")
    sensor._latest_reading_date = date_type(2026, 8, 14)  # two days stale

    assert sensor._is_data_fresh(nz_dt(2026, 8, 16, 7)) is False


def test_non_halfhourly_endpoints_are_always_fresh(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    """Endpoints without a characterized publish time never enter the retry ladder."""
    sensor = make_sensor(endpoint="dailywithstats")

    assert sensor._is_data_fresh(nz_dt(2026, 8, 16, 7)) is True


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (18, 0, True),  # the daily slot
        (18, 15, False),  # quarter slots are stale-retry only
        (0, 0, False),
        (6, 0, False),  # halfhourly has no secondary daily slot
        (12, 0, False),
    ],
)
def test_should_poll_now_fresh_halfhourly_polls_only_at_start_hour(
    make_sensor: Callable[..., WatercareUsageSensor],
    hour: int,
    minute: int,
    expected: bool,  # noqa: FBT001 -- pytest parametrization
) -> None:
    sensor = make_sensor(endpoint="halfhourly")
    now = utc_dt(2026, 8, 16, hour, minute)
    sensor._latest_reading_date = now.astimezone(NZ_TIMEZONE).date() - timedelta(days=1)

    assert sensor._should_poll_now(now) is expected


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (18, 0, True),
        (18, 15, True),  # quarter-hourly through the start hour
        (18, 45, True),
        (19, 0, True),  # then hourly
        (19, 15, False),
        (0, 0, True),  # the last retry hour (past midnight UTC) is still polled
        (0, 15, False),
        (1, 0, False),  # past the last retry: give up for the day
        (15, 0, False),  # before the window
    ],
)
def test_should_poll_now_stale_halfhourly_follows_retry_ladder(
    make_sensor: Callable[..., WatercareUsageSensor],
    hour: int,
    minute: int,
    expected: bool,  # noqa: FBT001 -- pytest parametrization
) -> None:
    sensor = make_sensor(endpoint="halfhourly")
    sensor._latest_reading_date = date_type(2026, 8, 14)  # stale

    assert sensor._should_poll_now(utc_dt(2026, 8, 16, hour, minute)) is expected


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (3, 0, True),  # hourly recovery retries run at any hour
        (14, 0, True),
        (3, 15, False),  # but only on the hour
    ],
)
def test_should_poll_now_retries_hourly_after_failed_update(
    make_sensor: Callable[..., WatercareUsageSensor],
    hour: int,
    minute: int,
    expected: bool,  # noqa: FBT001 -- pytest parametrization
) -> None:
    """
    Regression: a transient API failure must not leave the sensor stale all day.

    The SCAN_INTERVAL polling this scheduling replaced recovered from a
    failed poll within 12h; the failed-update flag drives hourly retries.
    """
    sensor = make_sensor(endpoint="halfhourly")
    now = utc_dt(2026, 8, 16, hour, minute)
    sensor._latest_reading_date = now.astimezone(NZ_TIMEZONE).date() - timedelta(
        days=1
    )  # data fresh
    sensor._last_update_failed = True

    assert sensor._should_poll_now(now) is expected


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (18, 0, True),
        (6, 0, True),  # secondary slot preserves the old ~12h cadence
        (19, 0, False),
        (0, 0, False),
    ],
)
def test_should_poll_now_other_endpoints_poll_twice_daily(
    make_sensor: Callable[..., WatercareUsageSensor],
    hour: int,
    minute: int,
    expected: bool,  # noqa: FBT001 -- pytest parametrization
) -> None:
    sensor = make_sensor(endpoint="dailywithstats")

    assert sensor._should_poll_now(utc_dt(2026, 8, 16, hour, minute)) is expected


@pytest.mark.parametrize(
    ("now_nz", "expected"),
    [
        (nz_dt(2026, 8, 16, 6), True),  # NZST (UTC+12): 18:00 UTC
        (nz_dt(2026, 8, 16, 7), False),
        (nz_dt(2026, 10, 13, 7), True),  # NZDT (UTC+13): 18:00 UTC
        (nz_dt(2026, 10, 13, 6), False),
    ],
    ids=["nzst-0600", "nzst-0700", "nzdt-0700", "nzdt-0600"],
)
def test_should_poll_now_daily_slot_tracks_utc_across_dst(
    make_sensor: Callable[..., WatercareUsageSensor],
    now_nz: datetime,
    expected: bool,  # noqa: FBT001 -- pytest parametrization
) -> None:
    """
    Regression: the daily slot must follow Watercare's UTC publish clock.

    Watercare publishes at ~18:15-18:45 UTC year-round. With the slots
    pinned to NZ wall-clock time, NZDT pushed the publish to 07:15-07:45
    local, past the whole 06:xx quarter-hourly window, so every day's data
    waited for the 08:00 hourly retry.
    """
    sensor = make_sensor(endpoint="halfhourly")
    sensor._latest_reading_date = now_nz.date() - timedelta(days=1)

    assert sensor._should_poll_now(now_nz) is expected


def test_schedule_polling_registers_quarter_hour_tick(
    make_sensor: Callable[..., WatercareUsageSensor],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensor = make_sensor()
    unsub_sentinel = MagicMock(name="unsub")
    fake_track_time_change = MagicMock(return_value=unsub_sentinel)
    monkeypatch.setattr(
        watercare_sensor, "async_track_time_change", fake_track_time_change
    )

    sensor._schedule_polling()

    fake_track_time_change.assert_called_once_with(
        sensor.hass,
        sensor._handle_scheduled_tick,
        minute=[0, 15, 30, 45],
        second=0,
    )
    assert sensor._unsub_scheduled_poll is unsub_sentinel


def test_schedule_polling_is_idempotent(
    make_sensor: Callable[..., WatercareUsageSensor],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a second call must not register a duplicate subscription."""
    sensor = make_sensor()
    unsub_sentinel = MagicMock(name="unsub")
    fake_track_time_change = MagicMock(return_value=unsub_sentinel)
    monkeypatch.setattr(
        watercare_sensor, "async_track_time_change", fake_track_time_change
    )

    sensor._schedule_polling()
    sensor._schedule_polling()

    fake_track_time_change.assert_called_once()
    assert sensor._unsub_scheduled_poll is unsub_sentinel


async def test_handle_tick_skips_entirely_when_not_subscribed(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    """Regression: a tick firing around entity removal must not touch the API."""
    sensor = make_sensor()
    sensor.async_update = AsyncMock()
    sensor.async_write_ha_state = MagicMock()

    await sensor._handle_scheduled_tick(nz_dt(2026, 8, 16, 6))

    sensor.async_update.assert_not_awaited()
    sensor.async_write_ha_state.assert_not_called()


async def test_handle_tick_skips_api_outside_poll_slots(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor(endpoint="halfhourly")
    sensor._unsub_scheduled_poll = MagicMock(name="unsub")
    sensor._latest_reading_date = date_type(2026, 8, 15)
    sensor.async_update = AsyncMock()
    sensor.async_write_ha_state = MagicMock()

    # Fresh data, mid-afternoon: not a poll slot.
    await sensor._handle_scheduled_tick(nz_dt(2026, 8, 16, 14))

    sensor.async_update.assert_not_awaited()
    sensor.async_write_ha_state.assert_not_called()


async def test_handle_tick_updates_and_writes_state_at_poll_slot(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor(endpoint="halfhourly")
    sensor._unsub_scheduled_poll = MagicMock(name="unsub")
    sensor._latest_reading_date = date_type(2026, 8, 15)
    sensor.async_update = AsyncMock()
    sensor.async_write_ha_state = MagicMock()

    await sensor._handle_scheduled_tick(nz_dt(2026, 8, 16, 6))

    sensor.async_update.assert_awaited_once()
    sensor.async_write_ha_state.assert_called_once()


async def test_handle_tick_logs_and_still_writes_state_on_failure(
    make_sensor: Callable[..., WatercareUsageSensor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression: a failed scheduled update must not abandon future polling."""
    sensor = make_sensor(endpoint="halfhourly")
    sensor._unsub_scheduled_poll = MagicMock(name="unsub")
    sensor._latest_reading_date = date_type(2026, 8, 15)
    sensor.async_update = AsyncMock(side_effect=RuntimeError("boom"))
    sensor.async_write_ha_state = MagicMock()

    with caplog.at_level(logging.ERROR):
        await sensor._handle_scheduled_tick(nz_dt(2026, 8, 16, 6))

    assert any(
        "Watercare scheduled update failed" in message for message in caplog.messages
    )
    sensor.async_write_ha_state.assert_called_once()


async def test_handle_tick_does_not_write_state_after_unsubscribe_mid_update(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    """
    Regression: removal during an in-flight update must not write state.

    async_will_remove_from_hass can run while the tick's async_update is
    awaiting the API; the cleared unsub handle signals the tick to stop.
    """
    sensor = make_sensor(endpoint="halfhourly")
    sensor._unsub_scheduled_poll = MagicMock(name="unsub")
    sensor._latest_reading_date = date_type(2026, 8, 15)

    async def _unsubscribe_mid_update() -> None:
        sensor._unsub_scheduled_poll = None

    sensor.async_update = AsyncMock(side_effect=_unsubscribe_mid_update)
    sensor.async_write_ha_state = MagicMock()

    await sensor._handle_scheduled_tick(nz_dt(2026, 8, 16, 6))

    sensor.async_update.assert_awaited_once()
    sensor.async_write_ha_state.assert_not_called()


async def test_run_backfill_schedules_polling_only_after_completing(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    """
    Regression: scheduling must wait for backfill, not race it.

    Registering the poll subscription from async_added_to_hass() (rather
    than from here, after backfill finishes) could let a scheduled poll
    fire mid-backfill and advance the statistics watermark out from under
    async_backfill_halfhourly_history() -- see its append-only warning.
    """
    sensor = make_sensor()
    sensor._entry.data = {}
    sensor.async_backfill_halfhourly_history = AsyncMock()
    sensor.async_update = AsyncMock()
    sensor.async_write_ha_state = MagicMock()
    sensor._schedule_polling = MagicMock()

    await sensor._run_backfill()

    sensor.async_backfill_halfhourly_history.assert_awaited_once()
    sensor._schedule_polling.assert_called_once()


async def test_run_backfill_schedules_polling_even_on_failure(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor()
    sensor._entry.data = {}
    sensor.async_backfill_halfhourly_history = AsyncMock(
        side_effect=RuntimeError("boom")
    )
    sensor.async_write_ha_state = MagicMock()
    sensor._schedule_polling = MagicMock()

    await sensor._run_backfill()

    sensor._schedule_polling.assert_called_once()


async def test_run_backfill_cancellation_skips_scheduling(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    """
    Regression: unloading the entry mid-backfill must not leave polling armed.

    The config entry cancels its background tasks on unload; the resulting
    CancelledError must propagate and skip both the state write and the
    subscription registration, or a defunct entity would keep polling the
    API (and racing its replacement's statistics watermark) forever.
    """
    sensor = make_sensor()
    sensor._entry.data = {}
    sensor.async_backfill_halfhourly_history = AsyncMock(
        side_effect=asyncio.CancelledError
    )
    sensor.async_write_ha_state = MagicMock()
    sensor._schedule_polling = MagicMock()

    with pytest.raises(asyncio.CancelledError):
        await sensor._run_backfill()

    sensor.async_write_ha_state.assert_not_called()
    sensor._schedule_polling.assert_not_called()


async def test_run_initial_update_schedules_polling_after_completing(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor()
    sensor.async_update = AsyncMock()
    sensor.async_write_ha_state = MagicMock()
    sensor._schedule_polling = MagicMock()

    await sensor._run_initial_update()

    sensor.async_update.assert_awaited_once()
    sensor._schedule_polling.assert_called_once()


async def test_run_initial_update_schedules_polling_even_on_failure(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor()
    sensor.async_update = AsyncMock(side_effect=RuntimeError("boom"))
    sensor.async_write_ha_state = MagicMock()
    sensor._schedule_polling = MagicMock()

    await sensor._run_initial_update()

    sensor._schedule_polling.assert_called_once()


async def test_run_initial_update_cancellation_skips_scheduling(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor()
    sensor.async_update = AsyncMock(side_effect=asyncio.CancelledError)
    sensor.async_write_ha_state = MagicMock()
    sensor._schedule_polling = MagicMock()

    with pytest.raises(asyncio.CancelledError):
        await sensor._run_initial_update()

    sensor.async_write_ha_state.assert_not_called()
    sensor._schedule_polling.assert_not_called()


async def test_will_remove_from_hass_cancels_subscription(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor()
    unsub = MagicMock(name="unsub")
    sensor._unsub_scheduled_poll = unsub

    await sensor.async_will_remove_from_hass()

    unsub.assert_called_once()
    assert sensor._unsub_scheduled_poll is None


async def test_will_remove_from_hass_is_a_noop_when_never_scheduled(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor()

    await sensor.async_will_remove_from_hass()

    assert sensor._unsub_scheduled_poll is None


def _patch_async_at_started(
    monkeypatch: pytest.MonkeyPatch, *, already_started: bool
) -> SimpleNamespace:
    """Replace async_at_started with a fake the test fires by hand."""
    fake = SimpleNamespace(callback=None, unsub=MagicMock())

    def fake_async_at_started(hass: Any, at_start_cb: Callable) -> Callable:
        if already_started:
            at_start_cb(hass)
        else:
            fake.callback = at_start_cb
        return fake.unsub

    monkeypatch.setattr(watercare_sensor, "async_at_started", fake_async_at_started)
    return fake


async def test_startup_job_waits_for_hass_started(
    make_sensor: Callable[..., WatercareUsageSensor],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Regression: the first fetch must not run while HA is still starting.

    The recorder doesn't process its queue until HA has started, so
    statistics pushed any earlier sat unwritten and
    _async_await_recorder_flush timed out with a warning on every restart.
    """
    sensor = make_sensor()
    fake = _patch_async_at_started(monkeypatch, already_started=False)
    startup_job = AsyncMock()

    task = asyncio.create_task(sensor._run_after_hass_started(startup_job))
    await asyncio.sleep(0)

    startup_job.assert_not_called()

    fake.callback(sensor.hass)
    await task

    startup_job.assert_awaited_once()
    fake.unsub.assert_called_once()


async def test_startup_job_runs_immediately_when_hass_already_started(
    make_sensor: Callable[..., WatercareUsageSensor],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensor = make_sensor()
    _patch_async_at_started(monkeypatch, already_started=True)
    startup_job = AsyncMock()

    await sensor._run_after_hass_started(startup_job)

    startup_job.assert_awaited_once()


async def test_startup_job_cancelled_before_hass_started_unsubscribes(
    make_sensor: Callable[..., WatercareUsageSensor],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unloading the entry mid-startup must drop the listener, not run the job."""
    sensor = make_sensor()
    fake = _patch_async_at_started(monkeypatch, already_started=False)
    startup_job = AsyncMock()

    task = asyncio.create_task(sensor._run_after_hass_started(startup_job))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    startup_job.assert_not_called()
    fake.unsub.assert_called_once()
