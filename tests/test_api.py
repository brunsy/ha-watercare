"""Tests for WatercareApi.get_data."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aioresponses import aioresponses

import custom_components.watercare.api as api_module
from custom_components.watercare.api import WatercareApi, WatercareAuthError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

BASE_URL = "https://customerapp.api.water.co.nz/"
MYACCOUNT_ACCOUNTS_URL = "https://api.watercare.co.nz/account/Accounts"


@pytest.fixture(autouse=True)
async def _clientsession(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[aiohttp.ClientSession]:
    """
    Stand in for HA's shared aiohttp session.

    api.py fetches its session via async_get_clientsession(hass), which
    needs a real HomeAssistant instance to cache against -- swap in a plain
    aiohttp.ClientSession instead so tests don't need a full hass fixture.
    """
    session = aiohttp.ClientSession()
    monkeypatch.setattr(api_module, "async_get_clientsession", lambda _hass: session)
    yield session
    await session.close()


def _make_api() -> WatercareApi:
    return WatercareApi(MagicMock(), "user@example.com", "hunter2")


async def test_get_data_invalid_endpoint_raises_before_any_network_call() -> None:
    api = _make_api()

    with pytest.raises(ValueError, match="Invalid endpoint"):
        await api.get_data(endpoint="not-a-real-endpoint")


async def test_get_data_returns_body_on_200_when_already_authenticated() -> None:
    api = _make_api()
    api._accountNumber = "123"
    api._token = "tok"

    with aioresponses() as mocked:
        mocked.get(f"{BASE_URL}v1/usage/123/halfhourly", status=200, body="usage-data")

        result = await api.get_data(endpoint="halfhourly")

    assert result == "usage-data"


async def test_get_data_appends_date_params_only_when_both_given() -> None:
    api = _make_api()
    api._accountNumber = "123"
    api._token = "tok"

    with aioresponses() as mocked:
        mocked.get(
            f"{BASE_URL}v1/usage/123/halfhourly?from=2026-01-01&to=2026-01-08",
            status=200,
            body="ranged",
        )

        result = await api.get_data(
            endpoint="halfhourly", start_date="2026-01-01", end_date="2026-01-08"
        )

    assert result == "ranged"


async def test_get_data_omits_date_params_when_only_one_given() -> None:
    api = _make_api()
    api._accountNumber = "123"
    api._token = "tok"

    with aioresponses() as mocked:
        # No query string registered -- if the code appended a partial
        # `?from=...` with no `to`, this mock wouldn't match and the test
        # would fail with an unmatched-request error.
        mocked.get(f"{BASE_URL}v1/usage/123/halfhourly", status=200, body="no-range")

        result = await api.get_data(endpoint="halfhourly", start_date="2026-01-01")

    assert result == "no-range"


async def test_get_data_non_200_returns_none_and_logs_status_and_body_separately(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api = _make_api()
    api._accountNumber = "123"
    api._token = "tok"

    with aioresponses() as mocked, caplog.at_level(logging.DEBUG):
        mocked.get(
            f"{BASE_URL}v1/usage/123/halfhourly",
            status=400,
            body='{"code":"INVALID_REQUEST_BODY"}',
        )

        result = await api.get_data(endpoint="halfhourly")

    assert result is None
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("400" in r.getMessage() for r in error_records)
    assert not any("INVALID_REQUEST_BODY" in r.getMessage() for r in error_records)
    assert any("INVALID_REQUEST_BODY" in r.getMessage() for r in debug_records)


async def test_get_data_triggers_refresh_token_when_unauthenticated() -> None:
    api = _make_api()
    assert api._accountNumber is None

    async def _fake_refresh_token(*_args: Any, **_kwargs: Any) -> None:
        api._accountNumber = "999"
        api._token = "fresh-token"

    api.get_refresh_token = AsyncMock(side_effect=_fake_refresh_token)

    with aioresponses() as mocked:
        mocked.get(f"{BASE_URL}v1/usage/999/halfhourly", status=200, body="authed")

        result = await api.get_data(endpoint="halfhourly")

    api.get_refresh_token.assert_awaited_once()
    assert result == "authed"


async def test_get_data_reauthenticates_and_retries_on_401() -> None:
    api = _make_api()
    api._accountNumber = "123"
    api._token = "stale-token"
    api._refresh_token = "some-refresh-token"

    async def _fake_get_api_token(*_args: Any, **_kwargs: Any) -> bool:
        api._token = "fresh-token"
        return True

    api.get_api_token = AsyncMock(side_effect=_fake_get_api_token)

    with aioresponses() as mocked:
        mocked.get(f"{BASE_URL}v1/usage/123/halfhourly", status=401, body="")
        mocked.get(f"{BASE_URL}v1/usage/123/halfhourly", status=200, body="retried-ok")

        result = await api.get_data(endpoint="halfhourly")

    api.get_api_token.assert_awaited_once()
    assert result == "retried-ok"


async def test_get_data_falls_back_to_full_login_when_refresh_grant_fails() -> None:
    """Regression: a failed refresh-token grant isn't treated as re-auth success."""
    api = _make_api()
    api._accountNumber = "123"
    api._token = "stale-token"
    api._refresh_token = "expired-refresh-token"

    async def _fake_get_api_token(*_args: Any, **_kwargs: Any) -> bool:
        return False  # refresh token itself has expired; _token untouched

    async def _fake_refresh_token(*_args: Any, **_kwargs: Any) -> None:
        api._accountNumber = "123"
        api._token = "fresh-token-from-full-login"

    api.get_api_token = AsyncMock(side_effect=_fake_get_api_token)
    api.get_refresh_token = AsyncMock(side_effect=_fake_refresh_token)

    with aioresponses() as mocked:
        mocked.get(f"{BASE_URL}v1/usage/123/halfhourly", status=401, body="")
        mocked.get(f"{BASE_URL}v1/usage/123/halfhourly", status=200, body="retried-ok")

        result = await api.get_data(endpoint="halfhourly")

    api.get_api_token.assert_awaited_once()
    api.get_refresh_token.assert_awaited_once()
    assert result == "retried-ok"


async def test_get_data_returns_none_when_reauth_after_401_fails() -> None:
    api = _make_api()
    api._accountNumber = "123"
    api._token = "stale-token"
    api._refresh_token = None

    async def _fake_refresh_token(*_args: Any, **_kwargs: Any) -> None:
        api._accountNumber = None  # re-authentication fails

    api.get_refresh_token = AsyncMock(side_effect=_fake_refresh_token)

    with aioresponses() as mocked:
        mocked.get(f"{BASE_URL}v1/usage/123/halfhourly", status=401, body="")

        result = await api.get_data(endpoint="halfhourly")

    api.get_refresh_token.assert_awaited_once()
    assert result is None


async def test_get_data_returns_none_when_authentication_fails() -> None:
    api = _make_api()

    api.get_refresh_token = AsyncMock(
        side_effect=lambda: None
    )  # never sets _accountNumber

    # No routes registered: if get_data() proceeded to make a usage request
    # despite failed auth, aioresponses would raise instead of letting a real
    # network call slip through.
    with aioresponses() as mocked:
        result = await api.get_data(endpoint="halfhourly")

    assert result is None
    mocked.assert_not_called()


class _FakeResponse:
    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self._text = text
        self.headers: dict[str, str] = {}

    async def text(self) -> str:
        return self._text

    async def json(self) -> Any:
        return json.loads(self._text)


class _FakeGetContextManager:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *_exc_info: object) -> bool:
        return False


async def test_get_refresh_token_raises_on_confirmed_sign_in_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2C confirming a real credential rejection must surface as WatercareAuthError."""
    api = _make_api()

    settings_response = _FakeResponse(
        200, 'var SETTINGS = {"transId": "tx123", "csrf": "csrf123"};'
    )
    confirmed_response = _FakeResponse(302, "")
    confirmed_response.headers["Location"] = (
        "msauth://nz.co.watercare/callback"
        "?error=access_denied&error_description=Bad+password"
    )
    get_responses = iter([settings_response, confirmed_response])

    def fake_get(_self: aiohttp.ClientSession, _url: str, **_kwargs: Any) -> Any:
        return _FakeGetContextManager(next(get_responses))

    def fake_post(_self: aiohttp.ClientSession, _url: str, **_kwargs: Any) -> Any:
        return _FakeGetContextManager(_FakeResponse(200, ""))

    monkeypatch.setattr(aiohttp.ClientSession, "get", fake_get)
    monkeypatch.setattr(aiohttp.ClientSession, "post", fake_post)

    with pytest.raises(WatercareAuthError, match="Bad password"):
        await api.get_refresh_token()


async def test_get_data_http_request_runs_with_lock_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the lock must only guard auth, not the HTTP request itself."""
    api = _make_api()
    api._accountNumber = "123"
    api._token = "tok"
    lock_states: list[bool] = []

    def fake_get(_self: aiohttp.ClientSession, _url: str, **_kwargs: Any) -> Any:
        lock_states.append(api._lock.locked())
        return _FakeGetContextManager(_FakeResponse(200, "ok"))

    monkeypatch.setattr(aiohttp.ClientSession, "get", fake_get)

    result = await api.get_data(endpoint="halfhourly")

    assert result == "ok"
    assert lock_states == [False]


async def test_get_accounts_uses_v1_account_when_it_works() -> None:
    api = _make_api()
    api._token = "tok"

    # Only /v1/account is registered: a MyAccount request would be an
    # unmatched call and fail the test.
    with aioresponses() as mocked:
        mocked.get(
            f"{BASE_URL}v1/account", status=200, payload=[{"accountNumber": "4242"}]
        )
        await api.get_accounts()

    assert api._accountNumber == "4242"


async def test_get_accounts_falls_back_to_myaccount_endpoint() -> None:
    """Regression: /v1/account 500ing while the MyAccount endpoint still works."""
    api = _make_api()
    api._token = "tok"

    with aioresponses() as mocked:
        mocked.get(f"{BASE_URL}v1/account", status=500, body='{"code":"UNKNOWN_ERROR"}')
        mocked.get(
            MYACCOUNT_ACCOUNTS_URL,
            status=200,
            payload=[{"accountNumber": "5150", "propertyAddress": "1 Example St"}],
        )
        await api.get_accounts()

    assert api._accountNumber == "5150"


@pytest.mark.parametrize(
    "v1_failure",
    [
        {"exception": aiohttp.ServerDisconnectedError()},
        {"exception": TimeoutError()},
        {"status": 200, "body": "<html>Down for maintenance</html>"},
        {"status": 200, "body": "not json", "content_type": "application/json"},
    ],
    ids=["disconnect", "timeout", "html-page", "invalid-json"],
)
async def test_get_accounts_falls_back_when_v1_account_raises(
    v1_failure: dict[str, Any],
) -> None:
    api = _make_api()
    api._token = "tok"

    with aioresponses() as mocked:
        mocked.get(f"{BASE_URL}v1/account", **v1_failure)
        mocked.get(
            MYACCOUNT_ACCOUNTS_URL, status=200, payload=[{"accountNumber": "5150"}]
        )
        await api.get_accounts()

    assert api._accountNumber == "5150"


async def test_get_accounts_logs_each_failed_endpoint_when_both_fail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api = _make_api()
    api._token = "tok"

    with aioresponses() as mocked, caplog.at_level(logging.WARNING):
        mocked.get(f"{BASE_URL}v1/account", status=500, body='{"code":"UNKNOWN_ERROR"}')
        mocked.get(MYACCOUNT_ACCOUNTS_URL, status=503, body="")
        await api.get_accounts()

    assert api._accountNumber is None
    messages = [r.getMessage() for r in caplog.records]
    assert any("status 500" in m for m in messages)
    assert any("status 503" in m for m in messages)
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_get_data_for_named_account_skips_default_account_lookup() -> None:
    api = _make_api()
    api._token = "tok"
    api.get_accounts = AsyncMock()

    with aioresponses() as mocked:
        mocked.get(f"{BASE_URL}v1/usage/777/halfhourly", status=200, body="usage")

        result = await api.get_data(endpoint="halfhourly", account_number="777")

    assert result == "usage"
    api.get_accounts.assert_not_awaited()


async def test_get_data_without_account_resolves_default_account_once() -> None:
    api = _make_api()
    api._token = "tok"

    with aioresponses() as mocked:
        mocked.get(f"{BASE_URL}v1/account", status=500, body="")
        mocked.get(
            MYACCOUNT_ACCOUNTS_URL,
            status=200,
            payload=[{"accountNumber": 9001}, {"accountNumber": 9002}],
        )
        mocked.get(f"{BASE_URL}v1/usage/9001/halfhourly", status=200, body="a")
        mocked.get(f"{BASE_URL}v1/usage/9001/halfhourly", status=200, body="b")

        assert await api.get_data(endpoint="halfhourly") == "a"
        # Second call: no further account requests (none registered).
        assert await api.get_data(endpoint="halfhourly") == "b"

    assert api.default_account_number == "9001"


async def test_login_does_not_look_up_accounts() -> None:
    """Accounts are chosen per subentry now; logging in alone mustn't need them."""
    api = _make_api()
    api._refresh_token = "refresh"
    api.get_accounts = AsyncMock()

    with aioresponses() as mocked:
        mocked.post(
            "https://wslpwb2cprd.b2clogin.com/tfp/wslpwb2cprd.onmicrosoft.com"
            "/B2C_1_sign_up_or_sign_in_mobile/oauth2/v2.0/token",
            status=200,
            payload={"access_token": "new-token"},
        )
        assert await api.get_api_token() is True

    api.get_accounts.assert_not_awaited()


async def test_async_get_accounts_logs_in_and_lists_all_accounts() -> None:
    api = _make_api()

    async def _fake_login(*_args: Any, **_kwargs: Any) -> None:
        api._token = "tok"

    api.get_refresh_token = AsyncMock(side_effect=_fake_login)

    with aioresponses() as mocked:
        mocked.get(f"{BASE_URL}v1/account", status=500, body="")
        mocked.get(
            MYACCOUNT_ACCOUNTS_URL,
            status=200,
            payload=[
                {"accountNumber": "1", "propertyAddress": "1 Example St"},
                {"accountNumber": "2"},
                {"accountName": "no number"},
            ],
        )
        accounts = await api.async_get_accounts()

    assert accounts == [
        {"account_number": "1", "address": "1 Example St"},
        {"account_number": "2", "address": None},
    ]


async def test_async_get_accounts_raises_when_login_yields_no_token() -> None:
    api = _make_api()
    api.get_refresh_token = AsyncMock()

    with pytest.raises(ValueError, match="access token"):
        await api.async_get_accounts()


async def test_get_data_renews_token_rejected_by_default_account_lookup() -> None:
    """
    Regression: an expired token must not wedge the default-account lookup.

    get_data() only renews a token when a usage request 401s, and it never
    reaches the usage request while the default account is unresolved --
    so the lookup has to renew a rejected token itself, or every poll fails
    until HA restarts.
    """
    api = _make_api()
    api._token = "stale-token"
    api._refresh_token = "some-refresh-token"

    async def _fake_get_api_token(*_args: Any, **_kwargs: Any) -> bool:
        api._token = "fresh-token"
        return True

    api.get_api_token = AsyncMock(side_effect=_fake_get_api_token)

    with aioresponses() as mocked:
        mocked.get(f"{BASE_URL}v1/account", status=401, body="")
        mocked.get(
            f"{BASE_URL}v1/account", status=200, payload=[{"accountNumber": 5150}]
        )
        mocked.get(f"{BASE_URL}v1/usage/5150/halfhourly", status=200, body="usage")

        result = await api.get_data(endpoint="halfhourly")

    api.get_api_token.assert_awaited_once()
    assert result == "usage"
    assert api.default_account_number == "5150"


async def test_account_lookup_skips_fallback_endpoint_on_401() -> None:
    """A rejected token is renewed rather than retried at the other endpoint."""
    api = _make_api()
    api._token = "stale-token"
    api._refresh_token = "some-refresh-token"

    async def _fake_get_api_token(*_args: Any, **_kwargs: Any) -> bool:
        api._token = "fresh-token"
        return True

    api.get_api_token = AsyncMock(side_effect=_fake_get_api_token)

    with aioresponses() as mocked:
        mocked.get(f"{BASE_URL}v1/account", status=401, body="")
        mocked.get(
            f"{BASE_URL}v1/account", status=200, payload=[{"accountNumber": 5150}]
        )
        # No MyAccount response registered: hitting it would raise.
        await api.get_accounts()

    assert api.default_account_number == "5150"


async def test_account_lookup_gives_up_when_renewed_token_is_rejected_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api = _make_api()
    api._token = "stale-token"
    api._refresh_token = "some-refresh-token"

    async def _fake_get_api_token(*_args: Any, **_kwargs: Any) -> bool:
        api._token = "fresh-token"
        return True

    api.get_api_token = AsyncMock(side_effect=_fake_get_api_token)

    with aioresponses() as mocked, caplog.at_level(logging.WARNING):
        mocked.get(f"{BASE_URL}v1/account", status=401, body="")
        mocked.get(f"{BASE_URL}v1/account", status=401, body="")

        result = await api.get_data(endpoint="halfhourly")

    api.get_api_token.assert_awaited_once()
    assert result is None
    assert api.default_account_number is None
    assert any("freshly renewed" in message for message in caplog.messages)


async def test_account_lookup_gives_up_when_token_renewal_fails() -> None:
    api = _make_api()
    api._token = "stale-token"
    api._refresh_token = None
    api.get_refresh_token = AsyncMock()  # full login leaves _token unchanged

    with aioresponses() as mocked:
        mocked.get(f"{BASE_URL}v1/account", status=401, body="")

        result = await api.get_data(endpoint="halfhourly")

    api.get_refresh_token.assert_awaited_once()
    assert result is None
    assert api.default_account_number is None
