"""Config flow for Watercare integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

from .const import (
    CONF_ANNUAL_LINE_CHARGE,
    CONF_CONSUMPTION_RATE,
    CONF_ENDPOINT,
    CONF_WASTEWATER_RATE,
    CONF_WASTEWATER_RATIO,
    DEFAULT_ANNUAL_LINE_CHARGE,
    DEFAULT_CONSUMPTION_RATE,
    DEFAULT_ENDPOINT,
    DEFAULT_WASTEWATER_RATE,
    DEFAULT_WASTEWATER_RATIO,
    DOMAIN,
    ENDPOINT_OPTIONS,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry, ConfigFlowResult

_LOGGER = logging.getLogger(__name__)

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_ENDPOINT, default=DEFAULT_ENDPOINT): vol.In(ENDPOINT_OPTIONS),
        vol.Optional(
            CONF_CONSUMPTION_RATE, default=DEFAULT_CONSUMPTION_RATE
        ): vol.Coerce(float),
        vol.Optional(CONF_WASTEWATER_RATE, default=DEFAULT_WASTEWATER_RATE): vol.Coerce(
            float
        ),
        vol.Optional(
            CONF_WASTEWATER_RATIO, default=DEFAULT_WASTEWATER_RATIO
        ): vol.Coerce(float),
        vol.Optional(
            CONF_ANNUAL_LINE_CHARGE, default=DEFAULT_ANNUAL_LINE_CHARGE
        ): vol.Coerce(float),
    }
)


class WatercareConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Watercare."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=DATA_SCHEMA,
            )

        return self.async_create_entry(
            title="Watercare",
            data={
                CONF_USERNAME: user_input[CONF_USERNAME],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
                CONF_ENDPOINT: user_input.get(CONF_ENDPOINT, DEFAULT_ENDPOINT),
                CONF_CONSUMPTION_RATE: user_input.get(
                    CONF_CONSUMPTION_RATE, DEFAULT_CONSUMPTION_RATE
                ),
                CONF_WASTEWATER_RATE: user_input.get(
                    CONF_WASTEWATER_RATE, DEFAULT_WASTEWATER_RATE
                ),
                CONF_WASTEWATER_RATIO: user_input.get(
                    CONF_WASTEWATER_RATIO, DEFAULT_WASTEWATER_RATIO
                ),
                CONF_ANNUAL_LINE_CHARGE: user_input.get(
                    CONF_ANNUAL_LINE_CHARGE, DEFAULT_ANNUAL_LINE_CHARGE
                ),
            },
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> WatercareOptionsFlowHandler:
        """Get the options flow for this handler."""
        return WatercareOptionsFlowHandler(config_entry)


class WatercareOptionsFlowHandler(config_entries.OptionsFlowWithConfigEntry):
    """Handle options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_ENDPOINT,
                        default=self.config_entry.data.get(
                            CONF_ENDPOINT, DEFAULT_ENDPOINT
                        ),
                    ): vol.In(ENDPOINT_OPTIONS),
                    vol.Optional(
                        CONF_CONSUMPTION_RATE,
                        default=self.config_entry.data.get(
                            CONF_CONSUMPTION_RATE, DEFAULT_CONSUMPTION_RATE
                        ),
                    ): vol.Coerce(float),
                    vol.Optional(
                        CONF_WASTEWATER_RATE,
                        default=self.config_entry.data.get(
                            CONF_WASTEWATER_RATE, DEFAULT_WASTEWATER_RATE
                        ),
                    ): vol.Coerce(float),
                    vol.Optional(
                        CONF_WASTEWATER_RATIO,
                        default=self.config_entry.data.get(
                            CONF_WASTEWATER_RATIO, DEFAULT_WASTEWATER_RATIO
                        ),
                    ): vol.Coerce(float),
                    vol.Optional(
                        CONF_ANNUAL_LINE_CHARGE,
                        default=self.config_entry.data.get(
                            CONF_ANNUAL_LINE_CHARGE, DEFAULT_ANNUAL_LINE_CHARGE
                        ),
                    ): vol.Coerce(float),
                }
            ),
        )
