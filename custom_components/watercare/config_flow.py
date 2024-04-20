"""Config flow for Watercare integration."""

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
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
                    CONF_EMAIL: user_input[CONF_EMAIL],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL, description="Enter your email"): cv.string,
                    vol.Required(CONF_PASSWORD, description="Enter your password"): cv.string,
                }
            ),
        )
