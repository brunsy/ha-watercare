# Watercare integration for Home Assistant

Monitor your water consumption and costs with detailed usage tracking, cost calculations, and Energy Dashboard integration.

## Data Source Options

Choose the appropriate data source based on your meter type:

### Non-Smart Meters
- **Monthly Billing Periods** (Default) - Shows billing period summaries from meter readings

### Smart Meters  
- **Daily Usage with Statistics** - Daily consumption with efficiency comparisons
- **Monthly Usage** - Monthly usage totals for trend analysis
- **Half-hourly Usage** - Detailed consumption data updated every 30 minutes

Most users with traditional meters should use the default option. Smart meter users can choose based on their preferred level of detail.

![Smart Meter Water Usage in Energy Dashboard](/homeassistant-water-graph.png "Energy Dashboard Reporting")
![Mechanical Meter Water Usage in Energy Dashboard](/homeassistant-water-graph-mechanicalmonthly.png "Energy Dashboard Reporting")

## Getting started

You will need an existing Watercare account. Smart meter users have access to detailed hourly and daily reporting, while traditional meter users can view monthly billing period summaries.

## Features

- **Multiple Data Sources**: Support for both smart and non-smart meters with different reporting frequencies
- **Cost Calculations**: Automatic cost calculations with configurable consumption and wastewater rates
- **Energy Dashboard Integration**: Seamless integration with Home Assistant's Energy Dashboard
- **Historical Statistics**: Generates long-term statistics for consumption and cost tracking
- **Efficiency Monitoring**: Smart meter users get household efficiency comparisons and usage insights

## Installation

Once installed, set up from the `Devices and services` area with the following configuration:

### Required Configuration
- **Username**: Your Watercare account username
- **Password**: Your Watercare account password

### Optional Configuration
- **Data Source**: Choose the appropriate endpoint for your meter type (see Data Source Options above)
- **Consumption Rate**: Cost per 1000L for water consumption (default: $2.296 NZD)
- **Wastewater Rate**: Cost per 1000L for wastewater processing (default: $3.994 NZD)

The rates can be configured during initial setup or modified later through the integration's options.

## Energy Dashboard Integration

This integration automatically creates statistics compatible with Home Assistant's Energy Dashboard:

- **Water Consumption**: Tracks total water usage over time
- **Water Costs**: Calculates and tracks total water costs
- **Consumption Costs**: Separate tracking of water consumption charges
- **Wastewater Costs**: Separate tracking of wastewater processing charges

The statistics are automatically generated and will appear in your Energy Dashboard's water section for long-term monitoring and analysis.

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

- Enhanced configuration flow labels and descriptions
- Additional cost analysis features
- Support for multiple properties/accounts
- More robust testing on waste-water charges, I do not have this on my account so was unable to test it

## Acknowledgements

This integration is not supported / endorsed by, nor affiliated with, Watercare.
