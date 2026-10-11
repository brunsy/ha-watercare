"""Tests for the config flow (login) and the account subentry flow."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from homeassistant.data_entry_flow import AbortFlow

from custom_components.watercare import config_flow as config_flow_module
from custom_components.watercare.api import WatercareAuthError
from custom_components.watercare.config_flow import (
    AccountSubentryFlow,
    WatercareConfigFlow,
)
from custom_components.watercare.const import (
    CONF_ACCOUNT_NUMBER,
    CONF_ENDPOINT,
    DEFAULT_CONSUMPTION_RATE,
    SUBENTRY_TYPE_ACCOUNT,
)

CREDENTIALS = {"username": "User@Example.com ", "password": "hunter2"}
ACCOUNTS = [
    {"account_number": "1111111111", "address": "1 Example St"},
    {"account_number": "2222222222", "address": None},
]


@pytest.fixture
def mock_api(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace WatercareApi in the config flow; sign-in lists ACCOUNTS."""
    api_cls = MagicMock()
    api_cls.return_value.async_get_accounts = AsyncMock(return_value=list(ACCOUNTS))
    monkeypatch.setattr(config_flow_module, "WatercareApi", api_cls)
    return api_cls


def _make_flow(source: str = "user") -> WatercareConfigFlow:
    flow = WatercareConfigFlow()
    flow.hass = MagicMock()
    flow.context = {"source": source}
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()
    return flow


def _field(result: dict[str, Any], name: str) -> Any:
    return next(
        (key, value)
        for key, value in result["data_schema"].schema.items()
        if key == name
    )


def _suggested(result: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description and "suggested_value" in key.description
    }


async def test_sign_in_uses_stripped_username(mock_api: MagicMock) -> None:
    """
    Regression: sign in with the username the form saves, not the raw input.

    Every form strips the username before saving it and using it as the
    unique id; signing in with the unstripped value would reject autofilled
    whitespace as invalid_auth even though the saved credentials work.
    """
    flow = _make_flow()

    await flow.async_step_user(dict(CREDENTIALS))

    mock_api.assert_called_once_with(flow.hass, "User@Example.com", "hunter2")


@pytest.mark.usefixtures("mock_api")
async def test_user_step_signs_in_then_asks_for_first_account() -> None:
    flow = _make_flow()

    result = await flow.async_step_user(dict(CREDENTIALS))

    assert result["type"] == "form"
    assert result["step_id"] == "account"
    flow.async_set_unique_id.assert_awaited_once_with("user@example.com")
    _key, selector = _field(result, CONF_ACCOUNT_NUMBER)
    options = selector.config["options"]
    assert [o["value"] for o in options] == ["1111111111", "2222222222"]
    assert options[0]["label"] == "1111111111 - 1 Example St"
    assert selector.config["custom_value"] is True


@pytest.mark.usefixtures("mock_api")
async def test_account_step_creates_entry_with_login_and_first_account() -> None:
    flow = _make_flow()
    await flow.async_step_user(dict(CREDENTIALS))

    result = await flow.async_step_account(
        {CONF_ACCOUNT_NUMBER: "1111111111", CONF_ENDPOINT: "monthly"}
    )

    assert result["type"] == "create_entry"
    assert result["title"] == "User@Example.com"
    assert result["data"] == {"username": "User@Example.com", "password": "hunter2"}
    (subentry,) = result["subentries"]
    assert subentry["subentry_type"] == SUBENTRY_TYPE_ACCOUNT
    assert subentry["unique_id"] == "1111111111"
    assert subentry["title"] == "1111111111 - 1 Example St"
    assert subentry["data"][CONF_ACCOUNT_NUMBER] == "1111111111"
    assert subentry["data"][CONF_ENDPOINT] == "monthly"
    assert subentry["data"]["consumption_rate"] == DEFAULT_CONSUMPTION_RATE


async def test_account_step_falls_back_to_typed_number_when_lookup_fails(
    mock_api: MagicMock,
) -> None:
    mock_api.return_value.async_get_accounts.return_value = []
    flow = _make_flow()

    result = await flow.async_step_user(dict(CREDENTIALS))

    _key, validator = _field(result, CONF_ACCOUNT_NUMBER)
    assert validator is str

    result = await flow.async_step_account({CONF_ACCOUNT_NUMBER: " 00123 45-02 "})
    assert result["type"] == "create_entry"
    # Spaces stripped, hyphen kept: it's part of the number the API expects.
    assert result["subentries"][0]["unique_id"] == "0012345-02"
    assert result["subentries"][0]["title"] == "0012345-02"


@pytest.mark.usefixtures("mock_api")
async def test_account_step_rejects_non_numeric_account_number() -> None:
    flow = _make_flow()
    await flow.async_step_user(dict(CREDENTIALS))

    result = await flow.async_step_account({CONF_ACCOUNT_NUMBER: "12-ab"})

    assert result["type"] == "form"
    assert result["errors"] == {CONF_ACCOUNT_NUMBER: "invalid_account_number"}


@pytest.mark.parametrize(
    ("side_effect", "error"),
    [
        (WatercareAuthError("bad password"), "invalid_auth"),
        (aiohttp.ClientConnectionError(), "cannot_connect"),
        (TimeoutError(), "cannot_connect"),
        (ValueError("no token"), "unknown"),
    ],
)
async def test_user_step_maps_sign_in_failures_to_form_errors(
    mock_api: MagicMock, side_effect: Exception, error: str
) -> None:
    mock_api.return_value.async_get_accounts.side_effect = side_effect

    result = await _make_flow().async_step_user(dict(CREDENTIALS))

    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": error}
    assert "password" not in _suggested(result)


def _entry(**overrides: Any) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "entry-1"
    entry.unique_id = "user@example.com"
    entry.data = {"username": "user@example.com", "password": "old"}
    entry.subentries = {}
    for key, value in overrides.items():
        setattr(entry, key, value)
    return entry


@pytest.mark.parametrize("source", ["reauth", "reconfigure"])
@pytest.mark.usefixtures("mock_api")
async def test_credentials_update_saves_and_relies_on_listener_to_reload(
    source: str,
) -> None:
    entry = _entry()
    flow = _make_flow(source)
    flow._get_reauth_entry = MagicMock(return_value=entry)
    flow._get_reconfigure_entry = MagicMock(return_value=entry)
    flow._abort_if_unique_id_mismatch = MagicMock()
    flow.hass.config_entries.async_update_entry.return_value = True
    step = (
        flow.async_step_reauth_confirm
        if source == "reauth"
        else flow.async_step_reconfigure
    )

    result = await step({"username": "user@example.com", "password": "new"})

    assert result["type"] == "abort"
    assert result["reason"] == f"{source}_successful"
    flow.hass.config_entries.async_update_entry.assert_called_once_with(
        entry, data={"username": "user@example.com", "password": "new"}
    )
    flow.hass.config_entries.async_schedule_reload.assert_not_called()


@pytest.mark.usefixtures("mock_api")
async def test_reauth_with_unchanged_credentials_still_reloads() -> None:
    entry = _entry()
    flow = _make_flow("reauth")
    flow._get_reauth_entry = MagicMock(return_value=entry)
    flow._abort_if_unique_id_mismatch = MagicMock()
    flow.hass.config_entries.async_update_entry.return_value = False

    await flow.async_step_reauth_confirm(
        {"username": "user@example.com", "password": "old"}
    )

    flow.hass.config_entries.async_schedule_reload.assert_called_once_with("entry-1")


@pytest.mark.usefixtures("mock_api")
async def test_credentials_update_refuses_a_different_login() -> None:
    flow = _make_flow("reconfigure")
    flow._get_reconfigure_entry = MagicMock(return_value=_entry())
    flow._abort_if_unique_id_mismatch = MagicMock(
        side_effect=AbortFlow("wrong_account")
    )

    with pytest.raises(AbortFlow, match="wrong_account"):
        await flow.async_step_reconfigure(
            {"username": "someone@else.com", "password": "x"}
        )

    flow.hass.config_entries.async_update_entry.assert_not_called()


async def test_credentials_rejected_are_not_saved(mock_api: MagicMock) -> None:
    mock_api.return_value.async_get_accounts.side_effect = WatercareAuthError("no")
    flow = _make_flow("reauth")
    flow._get_reauth_entry = MagicMock(return_value=_entry())
    flow._abort_if_unique_id_mismatch = MagicMock()

    result = await flow.async_step_reauth_confirm(
        {"username": "user@example.com", "password": "bad"}
    )

    assert result["errors"] == {"base": "invalid_auth"}
    flow.hass.config_entries.async_update_entry.assert_not_called()


def _subentry(unique_id: str | None, data: dict[str, Any] | None = None) -> MagicMock:
    subentry = MagicMock()
    subentry.unique_id = unique_id
    subentry.data = data or {}
    return subentry


def _make_subentry_flow(entry: MagicMock, source: str = "user") -> AccountSubentryFlow:
    flow = AccountSubentryFlow()
    flow.hass = MagicMock()
    flow.handler = (entry.entry_id, SUBENTRY_TYPE_ACCOUNT)
    flow.context = {"source": source}
    flow._get_entry = MagicMock(return_value=entry)
    return flow


async def test_add_account_lists_only_accounts_not_yet_configured(
    mock_api: MagicMock,
) -> None:
    entry = _entry(subentries={"s1": _subentry("1111111111")})
    flow = _make_subentry_flow(entry)

    result = await flow.async_step_user()

    _key, selector = _field(result, CONF_ACCOUNT_NUMBER)
    assert [o["value"] for o in selector.config["options"]] == ["2222222222"]
    mock_api.assert_called_once_with(flow.hass, "user@example.com", "old")


@pytest.mark.usefixtures("mock_api")
async def test_add_account_creates_subentry_keyed_by_account_number() -> None:
    flow = _make_subentry_flow(_entry())
    await flow.async_step_user()

    result = await flow.async_step_user(
        {CONF_ACCOUNT_NUMBER: "2222222222", CONF_ENDPOINT: "dailywithstats"}
    )

    assert result["type"] == "create_entry"
    assert result["unique_id"] == "2222222222"
    assert result["title"] == "2222222222"
    assert result["data"][CONF_ACCOUNT_NUMBER] == "2222222222"
    assert result["data"][CONF_ENDPOINT] == "dailywithstats"


@pytest.mark.usefixtures("mock_api")
async def test_add_account_rejects_typed_number_already_configured() -> None:
    entry = _entry(subentries={"s1": _subentry("1111111111")})
    flow = _make_subentry_flow(entry)
    await flow.async_step_user()

    result = await flow.async_step_user({CONF_ACCOUNT_NUMBER: "1111111111"})

    assert result["errors"] == {CONF_ACCOUNT_NUMBER: "account_already_configured"}


async def test_add_account_aborts_when_sign_in_fails(mock_api: MagicMock) -> None:
    mock_api.return_value.async_get_accounts.side_effect = WatercareAuthError("no")

    result = await _make_subentry_flow(_entry()).async_step_user()

    assert result["type"] == "abort"
    assert result["reason"] == "invalid_auth"


async def test_reconfigure_account_prefills_and_updates_settings(
    mock_api: MagicMock,
) -> None:
    subentry = _subentry(
        "1111111111", {CONF_ACCOUNT_NUMBER: "1111111111", CONF_ENDPOINT: "monthly"}
    )
    entry = _entry(subentries={"s1": subentry})
    flow = _make_subentry_flow(entry, source="reconfigure")
    flow._get_reconfigure_subentry = MagicMock(return_value=subentry)
    flow.async_update_and_abort = MagicMock(return_value={"type": "abort"})

    form = await flow.async_step_reconfigure()
    endpoint_key, _validator = _field(form, CONF_ENDPOINT)
    assert endpoint_key.default() == "monthly"
    assert CONF_ACCOUNT_NUMBER not in [str(k) for k in form["data_schema"].schema]

    await flow.async_step_reconfigure({CONF_ENDPOINT: "halfhourly"})

    flow.async_update_and_abort.assert_called_once()
    updates = flow.async_update_and_abort.call_args.kwargs["data_updates"]
    assert updates[CONF_ENDPOINT] == "halfhourly"
    assert CONF_ACCOUNT_NUMBER not in updates
    mock_api.assert_not_called()


async def test_add_account_aborts_when_every_account_is_configured(
    mock_api: MagicMock,
) -> None:
    entry = _entry(
        subentries={"s1": _subentry("1111111111"), "s2": _subentry("2222222222")}
    )

    result = await _make_subentry_flow(entry).async_step_user()

    assert result["type"] == "abort"
    assert result["reason"] == "no_unconfigured_accounts"
    mock_api.return_value.async_get_accounts.assert_awaited_once()


@pytest.mark.usefixtures("mock_api")
async def test_add_account_hides_default_account_of_unresolved_migrated_account() -> (
    None
):
    """A migrated account polls the default (first) account until resolved."""
    entry = _entry(subentries={"legacy": _subentry(None)})
    flow = _make_subentry_flow(entry)

    result = await flow.async_step_user()

    _key, selector = _field(result, CONF_ACCOUNT_NUMBER)
    assert [o["value"] for o in selector.config["options"]] == ["2222222222"]
    typed = await flow.async_step_user({CONF_ACCOUNT_NUMBER: "1111111111"})
    assert typed["errors"] == {CONF_ACCOUNT_NUMBER: "account_already_configured"}
