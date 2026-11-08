# Watercare integration for Home Assistant

Monitor your water consumption and costs with detailed usage tracking, cost calculations, and Energy Dashboard integration.

## Data Source Options

Choose the appropriate data source based on your meter type:

### Non-Smart Meters

- **Monthly Billing Periods** - Shows billing period summaries from meter readings

### Smart Meters

- **Half-hourly Usage** (Default)
- **Daily Usage with Statistics**
- **Monthly Usage**

Most users with traditional meters should use the default option. Smart meter users can choose based on their preferred level of detail.

![Smart Meter Water Usage in Energy Dashboard](/homeassistant-water-graph.png "Energy Dashboard Reporting")
![Mechanical Meter Water Usage in Energy Dashboard](/homeassistant-water-graph-mechanicalmonthly.png "Energy Dashboard Reporting")

## Getting started

You will need an existing Watercare account. Smart meter users have access to detailed hourly and daily reporting, while traditional meter users can view monthly billing period summaries.

## Installation

Once installed, set up from the `Devices and services` area. Setup has two steps:

### 1. Sign in

- **Email**: The email you use to sign in to the Watercare mobile app
- **Password**: Your Watercare mobile app password

The credentials are checked before continuing.

### 2. Add an account

Pick one of the accounts on your login (shown with its property address), or type the account number from your bill if it isn't listed. Each account has its own settings:

- **Data Source**: Choose the appropriate endpoint for your meter type (see Data Source Options above)
- **Consumption Rate**: Cost per 1000L for water consumption (default: $2.296 NZD)
- **Wastewater Rate**: Cost per 1000L for wastewater processing (default: $3.994 NZD)
- **Wastewater ratio**: Ratio of assumed wastewater usage by Watercare. Apartments are charged at 95 (default 0.785)
- **Annual Fixed charge**: Annual fixed charge per meter (default: $332 NZD)

### More accounts and later changes

- To track another property on the same login, open the integration and choose **Add account**. Each account gets its own sensor and statistics.
- To change an account's data source or rates, use **Reconfigure** on that account.
- To change the login's password, use **Reconfigure** on the integration itself. If Watercare rejects the saved credentials, Home Assistant prompts you to re-authenticate.

### Upgrading from an earlier version

An existing setup is converted automatically: its credentials stay on the integration and its data source and rates move onto an account. That account keeps the original `sensor.watercare` entity and statistic IDs, so existing history and Energy dashboard settings carry over. Accounts added afterwards get IDs that include the account number.

## Energy Dashboard Integration

This integration provides the following statistics for Home Assistant's Energy Dashboard:

- **Water Consumption**: Tracks total water usage over time
- **Water Costs**: Calculates and tracks total water costs
- **Consumption Costs**: Separate tracking of water consumption charges
- **Wastewater Costs**: Separate tracking of wastewater processing charges

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

## Acknowledgements

This integration is not supported / endorsed by, nor affiliated with, Watercare.
