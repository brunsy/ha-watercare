"""Config flow for Watercare integration."""

from homeassistant import config_entries
from homeassistant.const import CONF_TOKEN, CONF_CLIENT_ID
import homeassistant.helpers.config_validation as cv
import voluptuous as vol

from .const import DOMAIN, SENSOR_NAME

@config_entries.HANDLERS.register(DOMAIN)
class WatercareConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Define the config flow."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Show user form."""
        if user_input is not None:
            return self.async_create_entry(
                title=SENSOR_NAME,
                data={
                    CONF_CLIENT_ID: user_input[CONF_CLIENT_ID],
                    CONF_TOKEN: user_input[CONF_TOKEN],
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CLIENT_ID, description="Enter your client ID"): cv.string,
                    vol.Required(CONF_TOKEN, description="Enter your refresh token"): cv.string,
                }
            ),
        )
