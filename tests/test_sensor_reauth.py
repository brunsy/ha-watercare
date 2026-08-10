"""Tests for reauth triggering when Watercare rejects stored credentials."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from custom_components.watercare.api import WatercareAuthError

if TYPE_CHECKING:
    from collections.abc import Callable

    from custom_components.watercare.sensor import WatercareUsageSensor


async def test_get_data_starts_reauth_when_credentials_rejected(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor()
    sensor._api.get_data = AsyncMock(side_effect=WatercareAuthError("bad creds"))

    result = await sensor._get_data(
        endpoint="halfhourly", start_date=None, end_date=None
    )

    assert result is None
    sensor._entry.async_start_reauth.assert_called_once_with(sensor.hass)


async def test_get_data_clears_last_update_failed_on_confirmed_auth_rejection(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    """
    Regression: rejected credentials shouldn't keep the hourly retry ladder alive.

    _should_poll_now() polls hourly (any hour) whenever _last_update_failed
    is set -- but a confirmed credential rejection can't be fixed by
    retrying, only by the user completing reauth, so it must not leave that
    ladder spinning.
    """
    sensor = make_sensor()
    sensor._last_update_failed = True
    sensor._api.get_data = AsyncMock(side_effect=WatercareAuthError("bad creds"))

    await sensor._get_data(endpoint="halfhourly", start_date=None, end_date=None)

    assert sensor._last_update_failed is False


async def test_get_data_passes_through_response_without_starting_reauth(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    sensor = make_sensor()
    sensor._api.get_data = AsyncMock(return_value="usage-data")

    result = await sensor._get_data(
        endpoint="halfhourly", start_date=None, end_date=None
    )

    assert result == "usage-data"
    sensor._entry.async_start_reauth.assert_not_called()


async def test_get_data_only_starts_reauth_once_per_outage(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    """Regression: the retry ladder can call _get_data many times a day."""
    sensor = make_sensor()
    sensor._api.get_data = AsyncMock(side_effect=WatercareAuthError("bad creds"))

    await sensor._get_data(endpoint="halfhourly", start_date=None, end_date=None)
    await sensor._get_data(endpoint="halfhourly", start_date=None, end_date=None)
    await sensor._get_data(endpoint="halfhourly", start_date=None, end_date=None)

    sensor._entry.async_start_reauth.assert_called_once_with(sensor.hass)


async def test_get_data_starts_reauth_again_after_an_intervening_success(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    """A later success (e.g. after reauth completes) re-arms the guard."""
    sensor = make_sensor()
    sensor._api.get_data = AsyncMock(side_effect=WatercareAuthError("bad creds"))
    await sensor._get_data(endpoint="halfhourly", start_date=None, end_date=None)

    sensor._api.get_data = AsyncMock(return_value="usage-data")
    await sensor._get_data(endpoint="halfhourly", start_date=None, end_date=None)

    sensor._api.get_data = AsyncMock(side_effect=WatercareAuthError("bad creds"))
    await sensor._get_data(endpoint="halfhourly", start_date=None, end_date=None)

    assert sensor._entry.async_start_reauth.call_count == 2


async def test_get_data_unrelated_none_response_does_not_rearm_the_guard(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    """
    Regression: only a real success may clear _reauth_started.

    An unrelated transient failure (e.g. a non-200 from the usage endpoint)
    also returns None without raising WatercareAuthError -- that must not
    be mistaken for recovery and silently re-arm the reauth spam guard.
    """
    sensor = make_sensor()
    sensor._api.get_data = AsyncMock(side_effect=WatercareAuthError("bad creds"))
    await sensor._get_data(endpoint="halfhourly", start_date=None, end_date=None)

    sensor._api.get_data = AsyncMock(return_value=None)
    await sensor._get_data(endpoint="halfhourly", start_date=None, end_date=None)

    sensor._api.get_data = AsyncMock(side_effect=WatercareAuthError("bad creds"))
    await sensor._get_data(endpoint="halfhourly", start_date=None, end_date=None)

    sensor._entry.async_start_reauth.assert_called_once_with(sensor.hass)


async def test_async_update_skips_processing_while_reauth_is_pending(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    """
    Regression: don't spam a "no response" error every poll while reauth is pending.

    process_*'s _parse_json_response logs an ERROR whenever it's handed a
    None response -- useful for a genuine transient failure, but redundant
    (and spammy on the hourly recovery ladder) once reauth already told the
    user what's wrong, so async_update must skip processing in that case.
    """
    sensor = make_sensor()
    sensor._api.get_data = AsyncMock(side_effect=WatercareAuthError("bad creds"))
    sensor.process_halfhourly_data = AsyncMock()

    await sensor.async_update()

    sensor.process_halfhourly_data.assert_not_called()


async def test_async_update_still_processes_an_unrelated_none_response(
    make_sensor: Callable[..., WatercareUsageSensor],
) -> None:
    """A non-auth None response (e.g. a transient failure) must still be processed."""
    sensor = make_sensor()
    sensor._api.get_data = AsyncMock(return_value=None)
    sensor.process_halfhourly_data = AsyncMock()

    await sensor.async_update()

    sensor.process_halfhourly_data.assert_called_once_with(None)
