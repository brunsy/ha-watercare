"""Constants for Watercare integration."""

from zoneinfo import ZoneInfo

from homeassistant.const import Platform

# zoneinfo rather than pytz: pytz zones silently attach their historical
# LMT offset (NZ: +11:39, not +12:00) when passed directly as tzinfo= to
# the datetime constructor or replace(), a trap this codebase hit more
# than once; zoneinfo zones work correctly as plain tzinfo objects, and
# handle DST-gap/fold semantics without pytz's localize() machinery.
NZ_TIMEZONE = ZoneInfo("Pacific/Auckland")

DOMAIN = "watercare"
SENSOR_NAME = "Watercare"

# Configuration keys
CONF_CONSUMPTION_RATE = "consumption_rate"
CONF_WASTEWATER_RATE = "wastewater_rate"
CONF_WASTEWATER_RATIO = "wastewater_ratio"
CONF_ANNUAL_LINE_CHARGE = "annual_line_charge"
CONF_ENDPOINT = "endpoint"
CONF_HISTORY_BACKFILLED = "history_backfilled"

# Default cost rate per 1000L (NZD) - typical NZ Watercare rates
DEFAULT_CONSUMPTION_RATE = 2.296  # $2.296 per 1000L
DEFAULT_WASTEWATER_RATE = 3.994  # $3.994 per 1000L
DEFAULT_WASTEWATER_RATIO = 0.785  # 78.50% of water usage is wastewater
DEFAULT_ANNUAL_LINE_CHARGE = 310  # $310 per annum
DEFAULT_ENDPOINT = "halfhourly"

# Available API endpoints
ENDPOINT_OPTIONS = {
    "mechanicalmonthly": "Monthly Billing Periods (Non-Smart Meters)",
    "dailywithstats": "Daily Usage with Statistics (Smart Meters)",
    "monthly": "Monthly Usage (Smart Meters)",
    "halfhourly": "Half-hourly Usage (Smart Meters)",
}

# Endpoint display names for statistics
ENDPOINT_DISPLAY_NAMES = {
    "mechanicalmonthly": "Water",
    "dailywithstats": "Daily",
    "monthly": "Monthly",
    "halfhourly": "Half-hourly",
}

# Statistic type names
STATISTIC_TYPES = {
    "consumption": "Consumption",
    "cost": "Cost",
    "consumption_cost": "Consumption Cost",
    "wastewater_cost": "Wastewater Cost",
}

PLATFORMS = [
    Platform.SENSOR,
]
