"""Watercare API."""

import aiohttp
import logging
from datetime import datetime, timedelta
from typing import Any
from collections.abc import Mapping
import json
import secrets
import hashlib
import base64
import uuid
from urllib.parse import parse_qs

_LOGGER = logging.getLogger(__name__)

# http.client.HTTPConnection.debuglevel = 2
# logging.basicConfig()
# logging.getLogger().setLevel(logging.DEBUG)
# requests_log = logging.getLogger("requests.packages.urllib3")
# requests_log.setLevel(logging.DEBUG)
# requests_log.propagate = True

class WatercareApi:
    """Define the Watercare API."""

    def __init__(self, email, password):
        """Initialise the API."""
        self._client_id = "799c26af-c35b-4010-bd04-b6a7ebdba811"
        self._redirect_uri = 'msauth://nz.co.watercare/yRDm0vmCd9zdnwt1eCLGp8KfdLY%3D', #registered redirect URI for the client ID
        self._url_base = "https://customerapp.api.water.co.nz/"
        self._url_token_base = "https://wslpwb2cprd.b2clogin.com/tfp/wslpwb2cprd.onmicrosoft.com"
        self._p = "B2C_1_sign_up_or_sign_in_mobile"

        self._email = email
        self._password = password

        self._accountNumber = None
        self._token = None
        self._refresh_token = None
        self._refresh_token_expires_in = 0
        self._access_token_expires_in = 0

    def get_setting_json(self, page: str) -> Mapping[str, Any] | None:
        """Get the settings from json result."""
        for line in page.splitlines():
            if line.startswith("var SETTINGS = ") and line.endswith(";"):
                # Remove the prefix and suffix to get valid JSON
                json_string = line.removeprefix("var SETTINGS = ").removesuffix(";")
                return json.loads(json_string)
        return None

    def generate_code_verifier(self):
        """Generate code verifier for OAuth steps."""
        code_verifier = secrets.token_urlsafe(100)
        return code_verifier[:128]  # Trim to a maximum length of 128 characters

    def generate_code_challenge(self, code_verifier):
        """Generate code challenge for OAuth steps."""
        code_challenge = hashlib.sha256(code_verifier.encode()).digest()
        code_challenge_base64 = base64.urlsafe_b64encode(code_challenge).rstrip(b'=').decode()
        return code_challenge_base64

    async def get_refresh_token(self):
        """Get the refresh token."""
        _LOGGER.debug("API get_refresh_token")
        async with aiohttp.ClientSession() as session:
            url = f"{self._url_token_base}/{self._p}/oAuth2/v2.0/authorize"

            code_verifier = self.generate_code_verifier()
            code_challenge = self.generate_code_challenge(code_verifier)
            client_request_id = str(uuid.uuid4())
            scope = f'{self._client_id} openid offline_access profile'

            params = {
                'response_type': 'code',
                'code_challenge_method': 'S256',
                'client_id': self._client_id,
                'client-request-id': client_request_id,
                'scope': scope,
                'prompt': 'select_account',
                'redirect_uri': self._redirect_uri,
                'code_challenge': code_challenge
            }

            async with session.get(url, params=params) as response:
                response_text = await response.text()

            settings_json = self.get_setting_json(response_text)
            _LOGGER.debug(f"settings_json: {settings_json}")

            trans_id = settings_json.get("transId")
            csrf = settings_json.get("csrf")

            url = f"{self._url_token_base}/{self._p}/SelfAsserted?tx={trans_id}&p={self._p}"

            payload = {
                "request_type": "RESPONSE",
                "email": self._email,
                "password": self._password
            }

            headers = {
                'X-CSRF-TOKEN': csrf,
            }

            async with session.post(url, headers=headers, data=payload) as response:
                pass

            url = f"{self._url_token_base}/{self._p}/api/CombinedSigninAndSignup/confirmed"
            params = {
                'rememberMe': 'false',
                'csrf_token': csrf,
                'tx': trans_id,
                'p': self._p
            }

            headers = {}
            async with session.get(url, headers=headers, params=params, allow_redirects=False) as response:
                response.raise_for_status()
                location = response.headers.get('Location', '')
                query_params = parse_qs(location.split('?', 1)[1])
                if 'error' in query_params:
                    error = query_params['error'][0]
                    _LOGGER.error("Error in response: %s", error)
                    error_description = query_params['error_description'][0]
                    _LOGGER.error("Error description in response: %s", error_description)

            code = query_params['code'][0]

            url = f"{self._url_token_base}/{self._p}/oauth2/v2.0/token"
            params = {
                'client_id': self._client_id,
                'client-request-id': client_request_id,
                'client_info': 1,
                'code': code,
                'code_verifier': code_verifier,
                'grant_type': 'authorization_code',
                'scope': scope,
            }

            headers = {}
            async with session.get(url, headers=headers, params=params) as response:
                response_data = await response.json()
                refresh_token = response_data.get('refresh_token')
                access_token = response_data.get('access_token')
                refresh_token_expires_in = response_data.get('refresh_token_expires_in')
                access_token_expires_in = response_data.get('expires_in')

            self._token = access_token
            self._refresh_token = refresh_token
            self._refresh_token_expires_in = refresh_token_expires_in
            self._access_token_expires_in = access_token_expires_in
            _LOGGER.debug("Refresh token retrieved successfully.", {self._refresh_token})
            await self.get_accounts()


    async def get_api_token(self):
        """Get token from the Watercare API."""
        token_data = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "refresh_token": self._refresh_token,
        }

        async with aiohttp.ClientSession() as session:
            url = f"{self._url_token_base}/{self._p}/oauth2/v2.0/token"
            async with session.post(url, data=token_data) as response:
                if response.status == 200:
                    jsonResult = await response.json()
                    self._token = jsonResult["access_token"]
                    _LOGGER.debug(f"Authenticity Token: {self._token}")
                    await self.get_accounts()
                else:
                    _LOGGER.error("Failed to retrieve the token page.")

    async def get_accounts(self):
        """Get the first account that we see."""
        headers = {"authorization":  "Bearer " + self._token}
        async with aiohttp.ClientSession() as session, session.get(
            self._url_base + "v1/account",
            headers=headers
        ) as result:
            if result.status == 200:
                _LOGGER.debug("Retrieved accounts")
                data = await result.json()
                _LOGGER.debug(f"Accounts : {data}")
                if data and isinstance(data, list) and len(data) > 0:
                    first_account = data[0]
                    account_number = first_account.get("accountNumber")
                    if account_number is not None:
                        _LOGGER.debug(f"AccountNumber: {account_number}")
                        self._accountNumber = account_number
                    else:
                        _LOGGER.error("Account number not found in the response")
                else:
                    _LOGGER.error("No accounts found in the response")
            else:
                _LOGGER.error("Failed to fetch customer accounts %s", await result.text())

    async def get_data(self, endpoint: str):
        """Get data from the API."""
        if endpoint not in ["dailywithstats", "halfhourly"]:
            raise ValueError("Invalid endpoint. Must be 'dailywithstats' or 'halfhourly'.")

        if datetime.fromtimestamp(self._access_token_expires_in) <= datetime.now() + timedelta(minutes=5):
            _LOGGER.debug("Access token needs renewing")
            await self.get_api_token()

        if datetime.fromtimestamp(self._refresh_token_expires_in) <= datetime.now() + timedelta(hours=1):
            _LOGGER.debug("Refresh token needs renewing")
            await self.get_refresh_token()

        headers = {"authorization":  "Bearer " + self._token}

        today = datetime.now()
        seven_days_ago = today - timedelta(days=7) # fetch 7 days worth of data
        from_date = seven_days_ago.strftime("%Y-%m-%d") + "T00:00:00Z"
        to_date = today.strftime("%Y-%m-%d") + "T00:00:00Z"

        url = f"{self._url_base}v1/usage/{self._accountNumber}/{endpoint}?from={from_date}&to={to_date}"

        async with aiohttp.ClientSession() as session, \
                session.get(url, headers=headers) as response:
            if response.status == 200:
                data = await response.text()
                if not data:
                    _LOGGER.warning("Fetched consumption successfully but there was no data")
                return data
            else:
                _LOGGER.error("Could not fetch consumption")
                return None
