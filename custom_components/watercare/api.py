"""Watercare API."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_HTTP_OK = 200
_HTTP_UNAUTHORIZED = 401


class WatercareAuthError(Exception):
    """Raised when Watercare conclusively rejects the account credentials."""


class _AccessTokenRejectedError(Exception):
    """Raised when an account endpoint rejects the cached access token (401)."""


class WatercareApi:
    """Define the Watercare API."""

    def __init__(self, hass: HomeAssistant, email: str, password: str) -> None:
        """Initialise the API."""
        self._hass = hass
        self._client_id = "799c26af-c35b-4010-bd04-b6a7ebdba811"
        self._redirect_uri = "msauth://nz.co.watercare/yRDm0vmCd9zdnwt1eCLGp8KfdLY%3D"
        self._url_base = "https://customerapp.api.water.co.nz/"
        self._url_token_base = (
            "https://wslpwb2cprd.b2clogin.com/tfp/wslpwb2cprd.onmicrosoft.com"  # noqa: S105 -- URL host, not a credential
        )
        self._p = "B2C_1_sign_up_or_sign_in_mobile"

        self._email = email
        self._password = password

        self._accountNumber = None
        self._token = None
        self._refresh_token = None
        self._refresh_token_expires_in = 0
        self._access_token_expires_in = 0

        # Auth state (self._token, self._accountNumber, etc.) is mutable and
        # shared -- the regular poll and an on-demand backfill can both call
        # get_data() around the same time, so serialize access to avoid two
        # concurrent OAuth flows stomping on each other.
        self._lock = asyncio.Lock()

    def get_setting_json(self, page: str) -> Mapping[str, Any] | None:
        """Get the settings from json result."""
        for line in page.splitlines():
            if line.startswith("var SETTINGS = ") and line.endswith(";"):
                json_string = line.removeprefix("var SETTINGS = ").removesuffix(";")
                return json.loads(json_string)
        return None

    def generate_code_verifier(self) -> str:
        """Generate code verifier for OAuth steps."""
        code_verifier = secrets.token_urlsafe(100)
        return code_verifier[:128]

    def generate_code_challenge(self, code_verifier: str) -> str:
        """Generate code challenge for OAuth steps."""
        code_challenge = hashlib.sha256(code_verifier.encode()).digest()
        return base64.urlsafe_b64encode(code_challenge).rstrip(b"=").decode()

    async def get_refresh_token(self) -> None:  # noqa: PLR0915 -- linear OAuth flow, splitting would fragment the auth sequence
        """Get the refresh token."""
        _LOGGER.debug("API get_refresh_token")
        jar = aiohttp.CookieJar(quote_cookie=False)
        async with aiohttp.ClientSession(cookie_jar=jar) as session:
            url = f"{self._url_token_base}/{self._p}/oAuth2/v2.0/authorize"

            code_verifier = self.generate_code_verifier()
            code_challenge = self.generate_code_challenge(code_verifier)
            client_request_id = str(uuid.uuid4())
            scope = f"{self._client_id} openid offline_access profile"

            params = {
                "response_type": "code",
                "code_challenge_method": "S256",
                "client_id": self._client_id,
                "client-request-id": client_request_id,
                "scope": scope,
                "prompt": "select_account",
                "redirect_uri": self._redirect_uri,
                "code_challenge": code_challenge,
            }

            async with session.get(url, params=params) as response:
                response_text = await response.text()

            settings_json = self.get_setting_json(response_text)
            _LOGGER.debug("settings_json: %s", settings_json)

            trans_id = settings_json.get("transId")
            csrf = settings_json.get("csrf")

            url = (
                f"{self._url_token_base}/{self._p}/SelfAsserted"
                f"?tx={trans_id}&p={self._p}"
            )
            payload = {
                "request_type": "RESPONSE",
                "email": self._email,
                "password": self._password,
            }
            headers = {"X-CSRF-TOKEN": csrf}

            async with session.post(url, headers=headers, data=payload) as response:
                await response.text()

            url = (
                f"{self._url_token_base}/{self._p}"
                "/api/CombinedSigninAndSignup/confirmed"
            )
            params = {
                "rememberMe": "false",
                "csrf_token": csrf,
                "tx": trans_id,
                "p": self._p,
            }

            headers = {}
            async with session.get(
                url, headers=headers, params=params, allow_redirects=False
            ) as response:
                if response.status not in [200, 301, 302, 307, 308]:
                    response_text = await response.text()
                    _LOGGER.error(
                        "Failed to confirm sign in. Status: %s, Response: %s",
                        response.status,
                        response_text,
                    )
                    msg = f"Sign-in confirmation failed with status {response.status}"
                    raise ValueError(msg)

                location = response.headers.get("Location", "")
                if not location:
                    _LOGGER.error("No Location header in response")
                    msg = "No redirect location in sign-in response"
                    raise ValueError(msg)

                query_params = parse_qs(location.split("?", 1)[1])
                if "error" in query_params:
                    # DEBUG, not ERROR: every poll re-runs this full login
                    # while _accountNumber stays unset, so ERROR here would
                    # repeat every retry -- the raised WatercareAuthError
                    # (and the caller's single deduped warning) is the
                    # user-facing signal.
                    _LOGGER.debug("Error in response: %s", query_params["error"][0])
                    _LOGGER.debug(
                        "Error description: %s", query_params["error_description"][0]
                    )
                    msg = (
                        f"Authentication error: {query_params['error_description'][0]}"
                    )
                    raise WatercareAuthError(msg)

            code = query_params["code"][0]

            url = f"{self._url_token_base}/{self._p}/oauth2/v2.0/token"
            params = {
                "client_id": self._client_id,
                "client-request-id": client_request_id,
                "client_info": 1,
                "code": code,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "scope": scope,
            }

            headers = {}
            async with session.get(url, headers=headers, params=params) as response:
                response_data = await response.json()
                self._refresh_token = response_data.get("refresh_token")
                self._token = response_data.get("access_token")
                self._refresh_token_expires_in = response_data.get(
                    "refresh_token_expires_in"
                )
                self._access_token_expires_in = response_data.get("expires_in")

            _LOGGER.debug("Refresh token retrieved successfully.")

    async def get_api_token(self) -> bool:
        """Get token from the Watercare API. Returns whether it succeeded."""
        token_data = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "refresh_token": self._refresh_token,
        }

        session = async_get_clientsession(self._hass)
        url = f"{self._url_token_base}/{self._p}/oauth2/v2.0/token"
        async with session.post(url, data=token_data) as response:
            if response.status == _HTTP_OK:
                json_result = await response.json()
                self._token = json_result["access_token"]
                _LOGGER.debug("Access token retrieved successfully.")
                return True
            _LOGGER.error("Failed to retrieve the token page.")
            return False

    async def get_accounts(self) -> None:
        """
        Set the default account: the first one on the login.

        Only used by get_data() calls that don't name an account -- the
        account subentry migrated from a pre-subentry entry, until its
        account number has been resolved and stored.
        """
        accounts = await self._fetch_accounts()
        if accounts:
            self._accountNumber = accounts[0]["account_number"]
            _LOGGER.debug("AccountNumber: %s", self._accountNumber)
            return
        _LOGGER.error("Failed to fetch customer accounts")

    async def async_get_accounts(self) -> list[dict[str, str | None]]:
        """
        Log in if needed and return every account on the login.

        Each account is {"account_number": ..., "address": ...}; the address
        may be None. Raises WatercareAuthError if Watercare rejects the
        credentials. An empty list means signing in worked but neither
        account endpoint returned any accounts.
        """
        async with self._lock:
            if not self._token:
                await self.get_refresh_token()
            if not self._token:
                msg = "Sign-in completed without an access token"
                raise ValueError(msg)
            return await self._fetch_accounts()

    async def _fetch_accounts(self) -> list[dict[str, str | None]]:
        """
        Return the login's accounts, renewing a rejected access token once.

        Must be called with self._lock held. The usage requests in
        get_data() are the only other place a stale token gets renewed, and
        get_data() never reaches them while the default account is still
        unresolved -- so without renewing here, an access token that
        expired before the lookup first succeeded would be reused (and
        rejected) on every poll until HA restarted.
        """
        try:
            return await self._fetch_accounts_from_any()
        except _AccessTokenRejectedError:
            _LOGGER.debug("Account lookup rejected the access token; re-authenticating")
            if not await self._async_renew_token():
                return []
        try:
            return await self._fetch_accounts_from_any()
        except _AccessTokenRejectedError:
            _LOGGER.warning("Account lookup rejected a freshly renewed access token")
            return []

    async def _fetch_accounts_from_any(self) -> list[dict[str, str | None]]:
        """
        Return the login's accounts, trying each account endpoint in turn.

        /v1/account has been seen returning 500 {"code":"UNKNOWN_ERROR"} --
        for Watercare's own app too -- while the usage endpoints keep
        working, so fall back to the MyAccount website's account endpoint,
        which accepts the same mobile-app token and returns the same
        accountNumber field.
        """
        for url in (
            self._url_base + "v1/account",
            "https://api.watercare.co.nz/account/Accounts",
        ):
            try:
                accounts = await self._fetch_accounts_from(url)
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                # The outage behind the 500s can equally show up as a reset,
                # a timeout or a non-JSON maintenance page; still try the
                # next endpoint. (ValueError: body that isn't valid JSON.)
                _LOGGER.warning("Account lookup at %s failed: %r", url, err)
                continue
            if accounts:
                return accounts
        return []

    async def _fetch_accounts_from(self, url: str) -> list[dict[str, str | None]]:
        """Return the accounts from one account endpoint, or [] on failure."""
        headers = {"authorization": "Bearer " + (self._token or "")}
        session = async_get_clientsession(self._hass)
        async with session.get(url, headers=headers) as result:
            if result.status == _HTTP_UNAUTHORIZED:
                # The token, not this endpoint, is the problem: the other
                # endpoint would reject it too, so let _fetch_accounts()
                # renew it rather than trying the next one.
                raise _AccessTokenRejectedError
            if result.status != _HTTP_OK:
                _LOGGER.warning(
                    "Account lookup at %s failed (status %s): %s",
                    url,
                    result.status,
                    await result.text(),
                )
                return []
            data = await result.json()

        if not data or not isinstance(data, list):
            _LOGGER.warning("No accounts found in the response from %s", url)
            return []
        # Count only: the MyAccount response also carries names, addresses
        # and phone numbers, which have no business in debug logs.
        _LOGGER.debug("Account lookup at %s returned %s account(s)", url, len(data))
        accounts = [
            {
                "account_number": str(account["accountNumber"]),
                "address": account.get("propertyAddress") or account.get("address"),
            }
            for account in data
            if isinstance(account, dict) and account.get("accountNumber")
        ]
        if not accounts:
            _LOGGER.warning("Account number not found in the response from %s", url)
        return accounts

    async def get_data(
        self,
        endpoint: str,
        start_date: str | None = None,
        end_date: str | None = None,
        account_number: str | None = None,
    ) -> str | None:
        """
        Get usage data for `account_number`.

        Without an account number, uses the login's first account (looked
        up on first use) -- see get_accounts().
        """
        if endpoint not in [
            "halfhourly",
            "dailywithstats",
            "monthly",
            "mechanicalmonthly",
        ]:
            msg = "Invalid endpoint specified"
            raise ValueError(msg)

        if not await self._ensure_authenticated(
            need_default_account=account_number is None
        ):
            return None

        status, data, error_text = await self._request_usage(
            endpoint, start_date, end_date, account_number
        )
        if status == _HTTP_UNAUTHORIZED:
            # Access token expired/rejected -- get_data() previously only
            # re-authenticated when we had no account number at all, so once
            # logged in once, a stale token would be reused forever and every
            # later poll would 401. Re-authenticate once and retry.
            _LOGGER.debug("Access token rejected (401); re-authenticating and retrying")
            async with self._lock:
                if not await self._async_renew_token():
                    return None

            status, data, error_text = await self._request_usage(
                endpoint, start_date, end_date, account_number
            )

        if status != _HTTP_OK:
            _LOGGER.error("Could not fetch consumption: %s", status)
            _LOGGER.debug("Error response body: %s", error_text)
            return None
        return data

    async def _async_renew_token(self) -> bool:
        """
        Replace a rejected access token; return whether a new one was obtained.

        Must be called with self._lock held. The refresh-token grant can
        itself fail (e.g. the refresh token has expired) without raising --
        it just leaves _token as it was, which would otherwise look like a
        no-op success and retry with the same stale token. Fall back to a
        full login whenever it doesn't actually get us a fresh token.
        """
        stale_token = self._token
        refreshed = bool(self._refresh_token) and await self.get_api_token()
        if not refreshed:
            await self.get_refresh_token()
        if not self._token or self._token == stale_token:
            _LOGGER.error("Re-authentication failed - no new access token")
            return False
        return True

    async def _ensure_authenticated(self, *, need_default_account: bool) -> bool:
        """Log in if there's no access token, and resolve the default account."""
        # Serialize the whole re-authentication flow, not just the state
        # mutation: the regular poll and an on-demand backfill can both land
        # here around the same time, and we don't want two concurrent OAuth
        # logins racing over shared auth state. Only the single usage HTTP
        # request in _request_usage() runs outside the lock, so a
        # long-running backfill doesn't stall regular polling waiting on it.
        async with self._lock:
            if not self._token:
                _LOGGER.debug("No access token, starting authentication process")
                await self.get_refresh_token()
                if not self._token:
                    _LOGGER.error("Authentication failed - no access token obtained")
                    return False
            if need_default_account and not self._accountNumber:
                await self.get_accounts()
                if not self._accountNumber:
                    _LOGGER.error("Authentication failed - no account number obtained")
                    return False
            return True

    @property
    def default_account_number(self) -> str | None:
        """Return the login's first account, once get_accounts() has run."""
        return self._accountNumber

    async def _request_usage(
        self,
        endpoint: str,
        start_date: str | None,
        end_date: str | None,
        account_number: str | None,
    ) -> tuple[int, str | None, str | None]:
        """
        Make a single usage request against a snapshot of the auth state.

        Runs outside the lock -- it's read-only, and holding the lock here
        would stall a concurrent regular poll behind a long-running backfill
        request.
        """
        async with self._lock:
            account_number = account_number or self._accountNumber
            token = self._token

        headers = {"authorization": "Bearer " + (token or "")}

        url = f"{self._url_base}v1/usage/{account_number}/{endpoint}"
        if start_date and end_date:
            url += f"?from={start_date}&to={end_date}"

        _LOGGER.debug("Calling API URL: %s", url)

        session = async_get_clientsession(self._hass)
        async with session.get(url, headers=headers) as response:
            if response.status == _HTTP_OK:
                data = await response.text()
                _LOGGER.debug("API Response status: %s", response.status)
                _LOGGER.debug("API Response data length: %s", len(data) if data else 0)
                return response.status, data, None
            error_text = await response.text()
            return response.status, None, error_text
