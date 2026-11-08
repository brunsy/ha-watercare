"""
Config flow for Watercare integration.

The config entry holds the login (email + password); each Watercare account
on that login is a subentry with its own data source and rates. Initial
setup signs in, then adds the first account; further accounts are added via
the entry's "Add account" button (AccountSubentryFlow).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigFlow,
    ConfigSubentryFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import WatercareApi, WatercareAuthError
from .const import (
    ACCOUNT_SETTINGS_KEYS,
    CONF_ACCOUNT_NUMBER,
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
    SUBENTRY_TYPE_ACCOUNT,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import (
        ConfigEntry,
        ConfigFlowResult,
        SubentryFlowResult,
    )
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

_SETTINGS_DEFAULTS = {
    CONF_ENDPOINT: DEFAULT_ENDPOINT,
    CONF_CONSUMPTION_RATE: DEFAULT_CONSUMPTION_RATE,
    CONF_WASTEWATER_RATE: DEFAULT_WASTEWATER_RATE,
    CONF_WASTEWATER_RATIO: DEFAULT_WASTEWATER_RATIO,
    CONF_ANNUAL_LINE_CHARGE: DEFAULT_ANNUAL_LINE_CHARGE,
}

Account = dict[str, str | None]

# Watercare account numbers are digits with a hyphenated suffix, e.g.
# "0012345-02" -- the hyphen is part of the number the usage API expects.
_ACCOUNT_NUMBER_RE = re.compile(r"^\d+(-\d+)*$")


async def _async_sign_in(
    hass: HomeAssistant, credentials: Mapping[str, Any]
) -> tuple[dict[str, str], list[Account]]:
    """
    Sign in with `credentials` and list the login's accounts.

    Returns (errors, accounts). An empty account list with no errors means
    signing in worked but Watercare's account lookup didn't -- the account
    form then falls back to typing the number in.
    """
    # Sign in with the same stripped username the forms save, so stray
    # whitespace (e.g. from autofill) can't fail a sign-in that the saved
    # credentials would pass.
    api = WatercareApi(
        hass, credentials[CONF_USERNAME].strip(), credentials[CONF_PASSWORD]
    )
    try:
        return {}, await api.async_get_accounts()
    except WatercareAuthError:
        return {"base": "invalid_auth"}, []
    except aiohttp.ClientError, TimeoutError:
        return {"base": "cannot_connect"}, []
    except Exception:
        # Surface any other sign-in failure as a form error, not a crashed flow.
        _LOGGER.exception("Unexpected error signing in to Watercare")
        return {"base": "unknown"}, []


def _without_password(user_input: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return submitted form values to refill after an error, minus the password."""
    return {k: v for k, v in (user_input or {}).items() if k != CONF_PASSWORD}


def _account_label(account: Account) -> str:
    """Return how an account is shown: its number, plus address if known."""
    if account.get("address"):
        return f"{account['account_number']} - {account['address']}"
    return str(account["account_number"])


def _account_title(account_number: str, accounts: list[Account]) -> str:
    """Return the subentry title for `account_number`."""
    for account in accounts:
        if account["account_number"] == account_number:
            return _account_label(account)
    return account_number


def _validate_account_number(
    value: str, configured: set[str]
) -> tuple[str, dict[str, str]]:
    """Normalize a selected/typed account number; return it with any errors."""
    # Strip spaces a number copied from a bill may contain -- but not
    # hyphens, which are part of the number.
    account_number = "".join(value.split())
    if not _ACCOUNT_NUMBER_RE.match(account_number):
        return account_number, {CONF_ACCOUNT_NUMBER: "invalid_account_number"}
    if account_number in configured:
        return account_number, {CONF_ACCOUNT_NUMBER: "account_already_configured"}
    return account_number, {}


def _account_schema(
    accounts: list[Account] | None, defaults: Mapping[str, Any]
) -> vol.Schema:
    """
    Return the account form: which account (unless reconfiguring) + settings.

    accounts=None leaves the account field out (reconfiguring an existing
    account); an empty list asks for the number as free text (the lookup
    failed); otherwise it's a dropdown that also accepts a typed number.
    """
    fields: dict[Any, Any] = {}
    if accounts is not None:
        if accounts:
            fields[vol.Required(CONF_ACCOUNT_NUMBER)] = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(
                            value=str(account["account_number"]),
                            label=_account_label(account),
                        )
                        for account in accounts
                    ],
                    custom_value=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        else:
            fields[vol.Required(CONF_ACCOUNT_NUMBER)] = str

    def _default(key: str) -> Any:
        return defaults.get(key, _SETTINGS_DEFAULTS[key])

    fields[vol.Optional(CONF_ENDPOINT, default=_default(CONF_ENDPOINT))] = vol.In(
        ENDPOINT_OPTIONS
    )
    for key in (
        CONF_CONSUMPTION_RATE,
        CONF_WASTEWATER_RATE,
        CONF_WASTEWATER_RATIO,
        CONF_ANNUAL_LINE_CHARGE,
    ):
        fields[vol.Optional(key, default=_default(key))] = vol.Coerce(float)
    return vol.Schema(fields)


def _account_settings(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """Return the data source/rate settings from a submitted account form."""
    return {
        key: user_input.get(key, _SETTINGS_DEFAULTS[key])
        for key in ACCOUNT_SETTINGS_KEYS
    }


class WatercareConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Watercare."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize the flow."""
        self._credentials: dict[str, str] = {}
        self._accounts: list[Account] = []

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls,
        config_entry: ConfigEntry,  # noqa: ARG003 -- same subentry types for every entry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return the subentry types this integration supports."""
        return {SUBENTRY_TYPE_ACCOUNT: AccountSubentryFlow}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: sign in."""
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            await self.async_set_unique_id(username.lower())
            self._abort_if_unique_id_configured()
            errors, self._accounts = await _async_sign_in(self.hass, user_input)
            if not errors:
                self._credentials = {
                    CONF_USERNAME: username,
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                }
                return await self.async_step_account()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                CREDENTIALS_SCHEMA, _without_password(user_input)
            ),
            errors=errors,
        )

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: add the first account."""
        errors: dict[str, str] = {}
        if user_input is not None:
            account_number, errors = _validate_account_number(
                user_input[CONF_ACCOUNT_NUMBER], configured=set()
            )
            if not errors:
                return self.async_create_entry(
                    title=self._credentials[CONF_USERNAME],
                    data=self._credentials,
                    subentries=[
                        {
                            "subentry_type": SUBENTRY_TYPE_ACCOUNT,
                            "title": _account_title(account_number, self._accounts),
                            "unique_id": account_number,
                            "data": {
                                CONF_ACCOUNT_NUMBER: account_number,
                                **_account_settings(user_input),
                            },
                        }
                    ],
                )

        return self.async_show_form(
            step_id="account",
            data_schema=self.add_suggested_values_to_schema(
                _account_schema(self._accounts, {}), user_input or {}
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self,
        entry_data: Mapping[str, Any],  # noqa: ARG002 -- data lives on the reauth entry itself
    ) -> ConfigFlowResult:
        """Handle reauth, triggered when Watercare rejects stored credentials."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for new credentials and save them on the existing entry."""
        return await self._async_step_credentials(
            "reauth_confirm", self._get_reauth_entry(), user_input
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user change the login's credentials at any time."""
        return await self._async_step_credentials(
            "reconfigure", self._get_reconfigure_entry(), user_input
        )

    async def _async_step_credentials(
        self,
        step_id: str,
        entry: ConfigEntry,
        user_input: dict[str, Any] | None,
    ) -> ConfigFlowResult:
        """Shared credentials form for reauth and reconfigure."""
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            # The accounts (subentries) belong to this login: switching to a
            # different one would leave them pointing at accounts it may
            # not have access to.
            if entry.unique_id is not None:
                await self.async_set_unique_id(username.lower())
                self._abort_if_unique_id_mismatch(reason="wrong_account")
            errors, _accounts = await _async_sign_in(self.hass, user_input)
            if not errors:
                changed = self.hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        CONF_USERNAME: username,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
                # A data change reloads via the update listener; re-entering
                # the same credentials (e.g. after a Watercare-side outage)
                # still needs a reload to clear the pending reauth.
                if not changed:
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_abort(
                    reason="reauth_successful"
                    if self.source == SOURCE_REAUTH
                    else "reconfigure_successful"
                )

        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                CREDENTIALS_SCHEMA,
                {CONF_USERNAME: entry.data.get(CONF_USERNAME)}
                | _without_password(user_input),
            ),
            errors=errors,
        )


class AccountSubentryFlow(ConfigSubentryFlow):
    """Add a Watercare account to a login, or change an account's settings."""

    def __init__(self) -> None:
        """Initialize the flow."""
        self._accounts: list[Account] | None = None
        self._configured: set[str] = set()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Pick an account on the login that isn't set up yet."""
        entry = self._get_entry()
        if self._accounts is None:
            errors, accounts = await _async_sign_in(self.hass, entry.data)
            if errors:
                return self.async_abort(reason=errors["base"])
            self._configured = {
                subentry.unique_id
                for subentry in entry.subentries.values()
                if subentry.unique_id
            }
            # A migrated account that hasn't resolved its number yet polls
            # the login's default account -- the first one listed -- so
            # offering that one again would track it twice.
            if accounts and any(
                not subentry.unique_id for subentry in entry.subentries.values()
            ):
                self._configured.add(str(accounts[0]["account_number"]))
            self._accounts = [
                account
                for account in accounts
                if account["account_number"] not in self._configured
            ]
            # An empty list otherwise means the lookup failed and the number
            # has to be typed in; here there's simply nothing left to add.
            if accounts and not self._accounts:
                return self.async_abort(reason="no_unconfigured_accounts")

        errors = {}
        if user_input is not None:
            account_number, errors = _validate_account_number(
                user_input[CONF_ACCOUNT_NUMBER], self._configured
            )
            if not errors:
                return self.async_create_entry(
                    title=_account_title(account_number, self._accounts),
                    data={
                        CONF_ACCOUNT_NUMBER: account_number,
                        **_account_settings(user_input),
                    },
                    unique_id=account_number,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _account_schema(self._accounts, {}), user_input or {}
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change an account's data source and rates."""
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            # The entry's update listener reloads it to apply the change.
            return self.async_update_and_abort(
                entry, subentry, data_updates=_account_settings(user_input)
            )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_account_schema(None, subentry.data),
        )
