# Watercare integration for Home Assistant

View your water useage.

## Data
Sensor shows yesterdays water usage, but since the API exposes the current half hourly water usage this can be added in future

## Getting started
You will need to have an existing Watercare account and a watercare smart meter installed.
username/password is not yet implemented so currently you will need to intercept both the client_id and refresh_token from the watercare app to use this integration.

## Installation
Once installed, simply set-up from the `Devices and services` area. The first field is client_id and the next field is refresh_token for your account.

### HACS (recommended)
1. [Install HACS](https://hacs.xyz/docs/setup/download), if you did not already
2. [![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=brunsy&repository=ha-watercare&category=integration)
3. Install the Watercare integration
4. Restart Home Assistant

### Manually
Copy all files in the custom_components/watercare folder to your Home Assistant folder *config/custom_components/watercare*.

## Known issues

## Future enhancements
Your support is welcomed.

* Support for username/password setup
* Support for watercare rates (they only give us the monthly billing info, so will probably need to be set-up manually)

## Acknowledgements
This integration is not supported / endorsed by, nor affiliated with, Watercare.
