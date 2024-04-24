# Watercare integration for Home Assistant

View your water useage.

## Data

Sensor shows yesterdays water usage, but since the API exposes the current half hourly water usage this can be added in future

![Water Useage PNG](/homeassistant-water-graph.png "Energy Dashboard Reporting")

## Getting started

You will need to have an existing Watercare account and a Watercare smart meter installed for the hourly reporting.

## Installation

Once installed, simply set-up from the `Devices and services` area.
The first field is the username and the next field is the password for your Watercare account.

### HACS (recommended)

1. [Install HACS](https://hacs.xyz/docs/setup/download), if you did not already
2. [![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=brunsy&repository=ha-watercare&category=integration)
3. Install the Watercare integration
4. Restart Home Assistant

### Manually

Copy all files in the custom*components/watercare folder to your Home Assistant folder \_config/custom_components/watercare*.

## Known issues

## Future enhancements

Your support is welcomed.

- fix/add labels for user integration config flow
- Support for watercare rates (they only give us the monthly billing info, so will probably need to be set-up manually)

## Acknowledgements

This integration is not supported / endorsed by, nor affiliated with, Watercare.
