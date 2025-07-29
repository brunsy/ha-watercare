"""Watercare custom integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .api import WatercareApi

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Watercare from a config entry."""

    # Handle both old (email) and new (username) config formats
    email = entry.data.get("username") or entry.data.get("email")
    password = entry.data.get("password")

    if not email or not password:
        _LOGGER.error("Missing username/email or password in config entry")
        return False

    api = WatercareApi(email, password)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["api"] = api

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop("api")

    return unload_ok
