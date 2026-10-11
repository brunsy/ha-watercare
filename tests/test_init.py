"""Tests for entry migration, setup and the reload-on-settings-change listener."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from homeassistant.config_entries import ConfigSubentry

from custom_components.watercare import (
    _async_update_listener,
    _settings_snapshot,
    async_migrate_entry,
    async_setup_entry,
)
from custom_components.watercare.const import (
    CONF_ACCOUNT_NUMBER,
    CONF_HISTORY_BACKFILLED,
    CONF_LEGACY_IDS,
    DOMAIN,
    SUBENTRY_TYPE_ACCOUNT,
)


def _v1_entry(**overrides: Any) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "entry-1"
    entry.version = 1
    entry.unique_id = None
    entry.title = "Watercare"
    entry.data = {
        "username": "User@Example.com",
        "password": "hunter2",
        "endpoint": "halfhourly",
        "consumption_rate": 2.5,
        "wastewater_rate": 4.0,
        "wastewater_ratio": 0.8,
        "annual_line_charge": 300,
        CONF_HISTORY_BACKFILLED: True,
    }
    entry.options = {"consumption_rate": 2.9}
    for key, value in overrides.items():
        setattr(entry, key, value)
    return entry


async def test_migration_moves_settings_onto_legacy_account_subentry() -> None:
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = []
    entry = _v1_entry()

    assert await async_migrate_entry(hass, entry) is True

    (_entry, subentry), _ = hass.config_entries.async_add_subentry.call_args
    assert subentry.subentry_type == SUBENTRY_TYPE_ACCOUNT
    assert subentry.unique_id is None
    assert dict(subentry.data) == {
        "endpoint": "halfhourly",
        # options override data, as the v1 sensor did
        "consumption_rate": 2.9,
        "wastewater_rate": 4.0,
        "wastewater_ratio": 0.8,
        "annual_line_charge": 300,
        CONF_LEGACY_IDS: True,
        CONF_HISTORY_BACKFILLED: True,
    }
    hass.config_entries.async_update_entry.assert_called_once_with(
        entry,
        data={"username": "User@Example.com", "password": "hunter2"},
        options={},
        title="User@Example.com",
        unique_id="user@example.com",
        version=2,
    )


async def test_migration_handles_legacy_email_key_and_unique_id_clash() -> None:
    hass = MagicMock()
    clash = MagicMock()
    clash.unique_id = "old@example.com"
    hass.config_entries.async_entries.return_value = [clash]
    entry = _v1_entry(data={"email": "old@example.com", "password": "p"}, options={})

    await async_migrate_entry(hass, entry)

    kwargs = hass.config_entries.async_update_entry.call_args.kwargs
    assert kwargs["data"] == {"username": "old@example.com", "password": "p"}
    assert kwargs["unique_id"] is None


async def test_migration_strips_username_typed_with_stray_whitespace() -> None:
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = []
    entry = _v1_entry(
        data={"username": " Me@Example.com ", "password": "p"}, options={}
    )

    await async_migrate_entry(hass, entry)

    kwargs = hass.config_entries.async_update_entry.call_args.kwargs
    assert kwargs["data"]["username"] == "Me@Example.com"
    assert kwargs["unique_id"] == "me@example.com"
    assert kwargs["title"] == "Me@Example.com"


async def test_migration_refuses_unknown_future_version() -> None:
    assert await async_migrate_entry(MagicMock(), _v1_entry(version=3)) is False


def _subentry(data: dict[str, Any], subentry_id: str = "s1") -> ConfigSubentry:
    return ConfigSubentry(
        data=MappingProxyType(data),
        subentry_id=subentry_id,
        subentry_type=SUBENTRY_TYPE_ACCOUNT,
        title="Account",
        unique_id=data.get(CONF_ACCOUNT_NUMBER),
    )


def _v2_entry(subentries: dict[str, ConfigSubentry]) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "entry-1"
    entry.data = {"username": "u", "password": "p"}
    entry.subentries = subentries
    return entry


def _hass_with_snapshot(entry: MagicMock) -> MagicMock:
    hass = MagicMock()
    hass.data = {DOMAIN: {entry.entry_id: {"settings": _settings_snapshot(entry)}}}
    return hass


async def test_listener_ignores_runtime_bookkeeping_writes() -> None:
    """Regression: marking the backfill done mustn't reload mid-backfill."""
    entry = _v2_entry({"s1": _subentry({"endpoint": "halfhourly"})})
    hass = _hass_with_snapshot(entry)

    entry.subentries = {
        "s1": _subentry(
            {
                "endpoint": "halfhourly",
                CONF_HISTORY_BACKFILLED: True,
                CONF_ACCOUNT_NUMBER: "4242",
            }
        )
    }
    await _async_update_listener(hass, entry)

    hass.config_entries.async_schedule_reload.assert_not_called()


async def test_listener_reloads_on_settings_change() -> None:
    entry = _v2_entry({"s1": _subentry({"endpoint": "halfhourly"})})
    hass = _hass_with_snapshot(entry)

    entry.subentries = {"s1": _subentry({"endpoint": "monthly"})}
    await _async_update_listener(hass, entry)

    hass.config_entries.async_schedule_reload.assert_called_once_with("entry-1")


async def test_listener_reloads_when_an_account_is_added() -> None:
    entry = _v2_entry({"s1": _subentry({"endpoint": "halfhourly"})})
    hass = _hass_with_snapshot(entry)

    entry.subentries = {
        **entry.subentries,
        "s2": _subentry({CONF_ACCOUNT_NUMBER: "2"}, subentry_id="s2"),
    }
    await _async_update_listener(hass, entry)

    hass.config_entries.async_schedule_reload.assert_called_once_with("entry-1")


async def test_listener_reloads_on_credential_change() -> None:
    entry = _v2_entry({})
    hass = _hass_with_snapshot(entry)

    entry.data = {"username": "u", "password": "new"}
    await _async_update_listener(hass, entry)

    hass.config_entries.async_schedule_reload.assert_called_once_with("entry-1")


async def test_setup_registers_listener_and_settings_snapshot() -> None:
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    entry = _v2_entry({"s1": _subentry({"endpoint": "halfhourly"})})

    assert await async_setup_entry(hass, entry) is True

    entry_data = hass.data[DOMAIN]["entry-1"]
    assert entry_data["sensors"] == {}
    assert entry_data["settings"] == _settings_snapshot(entry)
    entry.add_update_listener.assert_called_once_with(_async_update_listener)


async def test_backfill_action_only_backfills_halfhourly_accounts() -> None:
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.services.has_service.return_value = False
    entry = _v2_entry({})
    await async_setup_entry(hass, entry)
    halfhourly, monthly = (
        MagicMock(endpoint="halfhourly"),
        MagicMock(endpoint="monthly"),
    )
    halfhourly.async_backfill_halfhourly_history = AsyncMock()
    monthly.async_backfill_halfhourly_history = AsyncMock()
    hass.data[DOMAIN]["entry-1"]["sensors"] = {"a": halfhourly, "b": monthly}
    handler = hass.services.async_register.call_args.args[2]

    await handler(MagicMock())

    halfhourly.async_backfill_halfhourly_history.assert_awaited_once()
    monthly.async_backfill_halfhourly_history.assert_not_awaited()
