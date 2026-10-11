"""Tests for per-account sensors: scoped ids, account routing, resolution."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.recorder.statistics import valid_statistic_id
from homeassistant.data_entry_flow import AbortFlow

from custom_components.watercare import sensor as watercare_sensor
from custom_components.watercare.const import CONF_ACCOUNT_NUMBER
from tests.conftest import nz_timestamp

if TYPE_CHECKING:
    from types import SimpleNamespace


def _account_sensor(
    account_number: str | None = "4242",
    *,
    legacy_ids: bool = False,
    endpoint: str = "dailywithstats",
) -> watercare_sensor.WatercareUsageSensor:
    subentry = MagicMock()
    subentry.data = {CONF_ACCOUNT_NUMBER: account_number} if account_number else {}
    sensor = watercare_sensor.WatercareUsageSensor(
        "Watercare",
        MagicMock(),
        2.296,
        3.994,
        0.785,
        310,
        endpoint,
        MagicMock(),
        needs_backfill=False,
        subentry=subentry,
        account_number=account_number,
        legacy_ids=legacy_ids,
    )
    sensor.hass = MagicMock()
    return sensor


def test_new_account_gets_account_scoped_ids_and_names() -> None:
    sensor = _account_sensor("4242")

    assert sensor.unique_id == "watercare_4242"
    assert sensor.name == "Watercare 4242"
    assert (
        sensor._statistic_id("daily_consumption") == "watercare:4242_daily_consumption"
    )
    assert sensor._get_statistic_name("consumption") == (
        "Watercare Daily Consumption (4242)"
    )


def test_hyphenated_account_number_yields_valid_statistic_ids() -> None:
    """Regression: real account numbers look like "0012345-02"."""
    sensor = _account_sensor("0012345-02")

    statistic_id = sensor._statistic_id("halfhourly_consumption")

    assert statistic_id == "watercare:0012345_02_halfhourly_consumption"
    assert valid_statistic_id(statistic_id)
    assert sensor.unique_id == "watercare_0012345-02"


def test_migrated_account_keeps_legacy_ids_and_names() -> None:
    sensor = _account_sensor("4242", legacy_ids=True)

    assert sensor.unique_id == "watercare"
    assert sensor.name == "Watercare"
    assert sensor._statistic_id("daily_consumption") == "watercare:daily_consumption"
    assert sensor._get_statistic_name("consumption") == "Watercare Daily Consumption"


async def test_daily_statistics_written_under_account_scoped_ids(
    patch_recorder: SimpleNamespace,
) -> None:
    sensor = _account_sensor("4242")
    response = json.dumps(
        {
            "usage": [
                {"timestamp": nz_timestamp(days_ago=2, hour=10), "litres": 300},
                {"timestamp": nz_timestamp(days_ago=1, hour=8), "litres": 210},
            ],
            "statistics": {},
        }
    )

    await sensor.process_daily_data(response)

    assert "watercare:4242_daily_consumption" in patch_recorder.written
    assert "watercare:daily_consumption" not in patch_recorder.written


async def test_get_data_requests_the_sensors_account() -> None:
    sensor = _account_sensor("4242")
    sensor._api.get_data = AsyncMock(return_value="usage")

    await sensor._get_data(endpoint="halfhourly", start_date="a", end_date="b")

    sensor._api.get_data.assert_awaited_once_with(
        endpoint="halfhourly", start_date="a", end_date="b", account_number="4242"
    )


async def test_migrated_subentry_stores_the_account_it_resolved_to() -> None:
    sensor = _account_sensor(None, legacy_ids=True)
    sensor._api.get_data = AsyncMock(return_value="usage")
    sensor._api.default_account_number = "4242"

    await sensor._get_data(endpoint="halfhourly", start_date=None, end_date=None)

    sensor.hass.config_entries.async_update_subentry.assert_called_once_with(
        sensor._entry,
        sensor._subentry,
        data={CONF_ACCOUNT_NUMBER: "4242"},
        title="4242",
        unique_id="4242",
    )
    assert sensor._account_number == "4242"
    # Still the legacy ids: resolving the number mustn't move its history.
    assert sensor._statistic_id("x") == "watercare:x"


async def test_resolved_account_clashing_with_another_subentry_only_warns() -> None:
    sensor = _account_sensor(None, legacy_ids=True)
    sensor._api.get_data = AsyncMock(return_value="usage")
    sensor._api.default_account_number = "4242"
    sensor.hass.config_entries.async_update_subentry.side_effect = AbortFlow(
        "already_configured"
    )

    assert (
        await sensor._get_data(endpoint="halfhourly", start_date=None, end_date=None)
        == "usage"
    )


async def test_failed_fetch_does_not_store_an_account() -> None:
    sensor = _account_sensor(None, legacy_ids=True)
    sensor._api.get_data = AsyncMock(return_value=None)
    sensor._api.default_account_number = "4242"

    await sensor._get_data(endpoint="halfhourly", start_date=None, end_date=None)

    sensor.hass.config_entries.async_update_subentry.assert_not_called()
