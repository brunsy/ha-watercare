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
