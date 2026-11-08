"""Watercare custom integration."""

from __future__ import annotations

import logging
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform

from .api import WatercareApi
from .const import (
    ACCOUNT_SETTINGS_KEYS,
    CONF_HISTORY_BACKFILLED,
    CONF_LEGACY_IDS,
    DOMAIN,
    SUBENTRY_TYPE_ACCOUNT,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, ServiceCall

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

SERVICE_BACKFILL_HISTORY = "backfill_history"


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """
    Migrate a v1 entry (one login, one implicit account) to login + subentries.

    v1 kept credentials, data source and rates all on the entry (rates
    possibly overridden in options). v2 keeps only credentials on the entry
    and moves the rest onto an account subentry. That subentry keeps the
    v1 entity unique_id and statistic ids (CONF_LEGACY_IDS) so existing
    history and Energy dashboard configuration carry over, and resolves
    its account number on the first successful poll.
    """
    if entry.version > 2:  # noqa: PLR2004 -- the version being migrated to
        # Downgraded from a future version this code doesn't understand.
        return False

    if entry.version == 1:
        merged = {**entry.data, **entry.options}
        account_data: dict[str, Any] = {
            key: merged[key] for key in ACCOUNT_SETTINGS_KEYS if key in merged
        }
        account_data[CONF_LEGACY_IDS] = True
        if entry.data.get(CONF_HISTORY_BACKFILLED):
            account_data[CONF_HISTORY_BACKFILLED] = True

        # v1 stored the username as typed; strip it like the v2 flows do, or
        # a stray autofill space would make every reauth a "wrong_account".
        username = (
            entry.data.get(CONF_USERNAME) or entry.data.get("email") or ""
        ).strip()
        unique_id = entry.unique_id
        if unique_id is None and username:
            candidate = username.lower()
            if not any(
                other.unique_id == candidate
                for other in hass.config_entries.async_entries(DOMAIN)
            ):
                unique_id = candidate

        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data=MappingProxyType(account_data),
                subentry_type=SUBENTRY_TYPE_ACCOUNT,
                title="Watercare account",
                unique_id=None,
            ),
        )
        hass.config_entries.async_update_entry(
            entry,
            data={
                CONF_USERNAME: username,
                CONF_PASSWORD: entry.data.get(CONF_PASSWORD),
            },
            options={},
            title=username or entry.title,
            unique_id=unique_id,
            version=2,
        )
        _LOGGER.debug("Migrated Watercare entry %s to version 2", entry.entry_id)

    return True


def _settings_snapshot(entry: ConfigEntry) -> dict[str, Any]:
    """
    Return the parts of an entry whose change needs a reload to take effect.

    Credentials, which accounts exist, and each account's data source and
    rates -- but not the bookkeeping sensors write back at runtime
    (CONF_HISTORY_BACKFILLED, a resolved account number/title), which
    would otherwise reload the entry mid-backfill.
    """
    return {
        "data": dict(entry.data),
        "accounts": {
            subentry_id: {key: subentry.data.get(key) for key in ACCOUNT_SETTINGS_KEYS}
            for subentry_id, subentry in entry.subentries.items()
        },
    }


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when settings or the set of accounts change (see _settings_snapshot)."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if entry_data and entry_data["settings"] != _settings_snapshot(entry):
        hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Watercare from a config entry."""
    email = entry.data.get(CONF_USERNAME)
    password = entry.data.get(CONF_PASSWORD)

    if not email or not password:
        _LOGGER.error("Missing username or password in config entry")
        return False

    api = WatercareApi(hass, email, password)

    hass.data.setdefault(DOMAIN, {})
    # Keyed by entry_id: each login gets its own api, shared by the sensors
    # of all its account subentries (filled in by the sensor platform).
    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "sensors": {},
        "settings": _settings_snapshot(entry),
    }
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _async_handle_backfill_history(call: ServiceCall) -> None:  # noqa: ARG001
        # Only accounts on the halfhourly data source: the backfill imports
        # halfhourly history, which other accounts' statistics don't use.
        sensors = [
            sensor
            for entry_data in hass.data.get(DOMAIN, {}).values()
            for sensor in entry_data.get("sensors", {}).values()
            if sensor.endpoint == "halfhourly"
        ]
        if not sensors:
            _LOGGER.error(
                "No Watercare account using the half-hourly data source is set up; "
                "nothing to backfill"
            )
            return
        for sensor in sensors:
            await sensor.async_backfill_halfhourly_history()

    if not hass.services.has_service(DOMAIN, SERVICE_BACKFILL_HISTORY):
        hass.services.async_register(
            DOMAIN, SERVICE_BACKFILL_HISTORY, _async_handle_backfill_history
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok
