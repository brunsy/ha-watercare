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
            await self.get_accounts()

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
                await self.get_accounts()
                return True
            _LOGGER.error("Failed to retrieve the token page.")
            return False

    async def get_accounts(self) -> None:
        """Get the first account that we see."""
        headers = {"authorization": "Bearer " + (self._token or "")}
        session = async_get_clientsession(self._hass)
        url = self._url_base + "v1/account"
        async with session.get(url, headers=headers) as result:
            if result.status == _HTTP_OK:
                data = await result.json()
                _LOGGER.debug("Accounts: %s", data)
                if data and isinstance(data, list) and len(data) > 0:
                    self._accountNumber = data[0].get("accountNumber")
                    if self._accountNumber:
                        _LOGGER.debug("AccountNumber: %s", self._accountNumber)
                    else:
                        _LOGGER.error("Account number not found in the response")
                else:
                    _LOGGER.error("No accounts found in the response")
            else:
                _LOGGER.error(
                    "Failed to fetch customer accounts %s", await result.text()
                )

    async def get_data(
        self, endpoint: str, start_date: str | None = None, end_date: str | None = None
    ) -> str | None:
        """Get data from the API."""
        if endpoint not in [
            "halfhourly",
            "dailywithstats",
            "monthly",
            "mechanicalmonthly",
        ]:
            msg = "Invalid endpoint specified"
            raise ValueError(msg)

        if not await self._ensure_authenticated():
            return None

        status, data, error_text = await self._request_usage(
            endpoint, start_date, end_date
        )
        if status == _HTTP_UNAUTHORIZED:
            # Access token expired/rejected -- get_data() previously only
            # re-authenticated when we had no account number at all, so once
            # logged in once, a stale token would be reused forever and every
            # later poll would 401. Re-authenticate once and retry.
            _LOGGER.debug("Access token rejected (401); re-authenticating and retrying")
            async with self._lock:
                # The refresh-token grant can itself fail (e.g. the refresh
                # token has expired) without raising -- it just leaves
                # _token/_accountNumber as they were, which would otherwise
                # look like a no-op success and retry with the same stale
                # token. Fall back to a full login whenever it doesn't
                # actually get us a fresh token.
                refreshed = bool(self._refresh_token) and await self.get_api_token()
                if not refreshed:
                    await self.get_refresh_token()
                if not self._accountNumber or not self._token:
                    _LOGGER.error(
                        "Re-authentication failed - no account number obtained"
                    )
                    return None

            status, data, error_text = await self._request_usage(
                endpoint, start_date, end_date
            )

        if status != _HTTP_OK:
            _LOGGER.error("Could not fetch consumption: %s", status)
            _LOGGER.debug("Error response body: %s", error_text)
            return None
        return data

    async def _ensure_authenticated(self) -> bool:
        """Authenticate if we don't yet have an account number."""
        # Serialize the whole re-authentication flow, not just the state
        # mutation: the regular poll and an on-demand backfill can both land
        # here around the same time, and we don't want two concurrent OAuth
        # logins racing over shared auth state. Only the single usage HTTP
        # request in _request_usage() runs outside the lock, so a
        # long-running backfill doesn't stall regular polling waiting on it.
        async with self._lock:
            if not self._accountNumber:
                _LOGGER.debug(
                    "No account number found, starting authentication process"
                )
                await self.get_refresh_token()
                if not self._accountNumber:
                    _LOGGER.error("Authentication failed - no account number obtained")
                    return False
            return True

    async def _request_usage(
        self,
        endpoint: str,
        start_date: str | None,
        end_date: str | None,
    ) -> tuple[int, str | None, str | None]:
        """
        Make a single usage request against a snapshot of the auth state.

        Runs outside the lock -- it's read-only, and holding the lock here
        would stall a concurrent regular poll behind a long-running backfill
        request.
        """
        async with self._lock:
            account_number = self._accountNumber
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
