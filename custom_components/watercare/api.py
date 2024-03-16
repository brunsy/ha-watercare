import aiohttp
import logging
from datetime import datetime, timedelta

_LOGGER = logging.getLogger(__name__)

class WatercareApi:
    """Define the Watercare API."""

    def __init__(self, client_id, refresh_token):
        """Initialise the API."""
        self._client_id = client_id
        self._refresh_token = refresh_token
        self._url_base = "https://customerapp.api.water.co.nz/"
        self._url_token_base = "https://wslpwb2cprd.b2clogin.com/tfp/wslpwb2cprd.onmicrosoft.com/"
        self._token = None
        self._accountNumber = None
        self._data = None

    async def token(self):
        """Get token from the Watercare API."""
        token_data = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "refresh_token": self._refresh_token,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                self._url_token_base + "B2C_1_sign_up_or_sign_in_mobile/%2FoAuth2%2Fv2.0%2Ftoken",
                data=token_data
            ) as response:
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
        async with aiohttp.ClientSession() as session:
            async with session.get(
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
    
        today = datetime.now()
        seven_days_ago = today - timedelta(days=7)
        headers = {"authorization":  "Bearer " + self._token}

        async with aiohttp.ClientSession() as session:
            async with session.get(
                self._url_base
                + "v1/usage/"
                + self._accountNumber
                + f"/{endpoint}?from="
                + seven_days_ago.strftime("%Y-%m-%dT")+ "00:00:00Z"
                + "&to="
                + today.strftime("%Y-%m-%dT")+ "00:00:00Z",
                headers=headers
            ) as response:
                if response.status == 200:
                    data = await response.text()
                    if not data:
                        _LOGGER.warning(
                            "Fetched consumption successfully but there was no data"
                        )
                    return data
                else:
                    _LOGGER.error("Could not fetch consumption")
                    return None
