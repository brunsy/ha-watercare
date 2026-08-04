"""Watercare sensors."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.components.sensor import SensorEntity

from .const import (
    CONF_ANNUAL_LINE_CHARGE,
    CONF_CONSUMPTION_RATE,
    CONF_ENDPOINT,
    CONF_WASTEWATER_RATE,
    CONF_WASTEWATER_RATIO,
    DEFAULT_ANNUAL_LINE_CHARGE,
    DEFAULT_CONSUMPTION_RATE,
    DEFAULT_ENDPOINT,
    DEFAULT_WASTEWATER_RATE,
    DEFAULT_WASTEWATER_RATIO,
    DOMAIN,
    ENDPOINT_DISPLAY_NAMES,
    NZ_TIMEZONE,
    SENSOR_NAME,
    STATISTIC_TYPES,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .api import WatercareApi

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(hours=12)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> bool:
    """Set up the Watercare sensor platform."""
    if "api" not in hass.data[DOMAIN]:
        _LOGGER.error("API instance not found in config entry data.")
        return False

    api = hass.data[DOMAIN]["api"]

    # Get rates and endpoint from config entry data
    consumption_rate = entry.data.get(CONF_CONSUMPTION_RATE, DEFAULT_CONSUMPTION_RATE)
    wastewater_rate = entry.data.get(CONF_WASTEWATER_RATE, DEFAULT_WASTEWATER_RATE)
    wastewater_ratio = entry.data.get(CONF_WASTEWATER_RATIO, DEFAULT_WASTEWATER_RATIO)
    annual_line_charge = entry.data.get(
        CONF_ANNUAL_LINE_CHARGE, DEFAULT_ANNUAL_LINE_CHARGE
    )
    endpoint = entry.data.get(CONF_ENDPOINT, DEFAULT_ENDPOINT)

    # Check for updated values in options
    if entry.options:
        consumption_rate = entry.options.get(CONF_CONSUMPTION_RATE, consumption_rate)
        wastewater_rate = entry.options.get(CONF_WASTEWATER_RATE, wastewater_rate)
        wastewater_ratio = entry.options.get(CONF_WASTEWATER_RATIO, wastewater_ratio)
        annual_line_charge = entry.options.get(
            CONF_ANNUAL_LINE_CHARGE, annual_line_charge
        )
        endpoint = entry.options.get(CONF_ENDPOINT, endpoint)

    async_add_entities(
        [
            WatercareUsageSensor(
                SENSOR_NAME,
                api,
                consumption_rate,
                wastewater_rate,
                wastewater_ratio,
                annual_line_charge,
                endpoint,
            )
        ],
        update_before_add=True,
    )

    return True


class WatercareUsageSensor(SensorEntity):
    """Define Watercare Usage sensor."""

    def __init__(  # noqa: PLR0913 -- mirrors the integration's config options; grouping into a dataclass would ripple through call sites for no behavior change
        self,
        name: str,
        api: WatercareApi,
        consumption_rate: float,
        wastewater_rate: float,
        wastewater_ratio: float,
        annual_line_charge: float,
        endpoint: str,
    ) -> None:
        """Initialize Watercare Usage sensor."""
        self._name = name
        self._icon = "mdi:water"
        self._state = None
        self._unit_of_measurement = "L"
        self._unique_id = DOMAIN
        self._device_class = "water"
        self._state_class = "total_increasing"
        self._state_attributes = {}
        self._api = api
        self._consumption_rate = consumption_rate
        self._wastewater_rate = wastewater_rate
        self._wastewater_ratio = wastewater_ratio
        self._annual_line_charge = annual_line_charge
        self._endpoint = endpoint

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return self._name

    @property
    def icon(self) -> str:
        """Icon to use in the frontend, if any."""
        return self._icon

    @property
    def state(self) -> float | None:
        """Return the state of the device."""
        return self._state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes of the sensor."""
        return self._state_attributes

    @property
    def unit_of_measurement(self) -> str:
        """Return the unit of measurement."""
        return self._unit_of_measurement

    @property
    def state_class(self) -> str:
        """Return the state class."""
        return self._state_class

    @property
    def device_class(self) -> str:
        """Return the device class."""
        return self._device_class

    @property
    def unique_id(self) -> str:
        """Return the unique id."""
        return self._unique_id

    def _calculate_cost(
        self, usage_litres: float, number_of_days: int
    ) -> dict[str, float]:
        """Calculate the total cost based on usage and configured rates."""
        usage_thousands = usage_litres / 1000.0

        # Calculate cost components
        consumption_cost = usage_thousands * self._consumption_rate
        wastewater_cost = (
            usage_thousands * self._wastewater_rate * self._wastewater_ratio
        )
        line_charge = (self._annual_line_charge / 365) * number_of_days
        total_cost = consumption_cost + wastewater_cost + line_charge

        return {
            "total": total_cost,
            "consumption": consumption_cost,
            "wastewater": wastewater_cost,
            "line_charge": DEFAULT_ANNUAL_LINE_CHARGE / 365,
        }

    def _get_statistic_name(self, statistic_type: str) -> str:
        """Generate consistent statistic names based on endpoint and type."""
        endpoint_name = ENDPOINT_DISPLAY_NAMES.get(
            self._endpoint, self._endpoint.title()
        )
        type_name = STATISTIC_TYPES.get(statistic_type, statistic_type.title())
        return f"Watercare {endpoint_name} {type_name}"

    async def async_update(self) -> None:
        """Update the sensor data."""
        _LOGGER.debug("Beginning sensor update using endpoint: %s", self._endpoint)
        response = await self._api.get_data(endpoint=self._endpoint)

        # Route to appropriate processing method based on endpoint
        if self._endpoint == "dailywithstats":
            await self.process_daily_data(response)
        else:
            # For mechanicalmonthly, monthly, and halfhourly, use the
            # billing period processing.
            await self.process_data(response)

    async def process_data(self, response: str | None) -> None:
        """Process the API response."""
        if response is None:
            _LOGGER.error(
                "No response received from Watercare API; skipping processing"
            )
            return

        try:
            billing_periods = json.loads(response)
        except json.JSONDecodeError:
            _LOGGER.exception("Failed to parse Watercare API response")
            return

        _LOGGER.debug("Processing data: %s", billing_periods)

        if not billing_periods:
            _LOGGER.warning("No billing periods found")
            return

        # Get the most recent billing period for current usage
        latest_period = billing_periods[0]
        daily_average = latest_period.get("statistics", {}).get("dailyAverage", 0)

        # Set the sensor state to cumulative usage for Energy Dashboard
        billing_period_usage = latest_period.get("waterUsage", 0)
        self._state = billing_period_usage

        number_of_days = (
            datetime.strptime(
                latest_period.get("billingPeriodToDate"), "%Y-%m-%dT%H:%M:%S.%fZ"
            ).replace(tzinfo=UTC)
            - datetime.strptime(
                latest_period.get("billingPeriodFromDate"), "%Y-%m-%dT%H:%M:%S.%fZ"
            ).replace(tzinfo=UTC)
        ).days + 1

        cost_breakdown = self._calculate_cost(billing_period_usage, number_of_days)

        self._state_attributes = {
            "billing_period_usage": billing_period_usage,
            "daily_average": daily_average,
            "billing_period_from": latest_period.get("billingPeriodFromDate"),
            "billing_period_to": latest_period.get("billingPeriodToDate"),
            "reading_type": latest_period.get("readingType"),
            "account_balance": latest_period.get("accountBalance"),
            "amount_due": latest_period.get("amountDue"),
            "household_efficiency_band": latest_period.get("statistics", {})
            .get("efficiency", {})
            .get("currentHouseholdBand"),
            "usage_to_lower_band": latest_period.get("statistics", {})
            .get("efficiency", {})
            .get("usageToLowerBand"),
            "current_period_cost": round(cost_breakdown["total"], 2),
            "current_period_cost_consumption": round(cost_breakdown["consumption"], 2),
            "current_period_cost_wastewater": round(cost_breakdown["wastewater"], 2),
            "consumption_rate_per_1000L": self._consumption_rate,
            "wastewater_rate_per_1000L": self._wastewater_rate,
            "endpoint": self._endpoint,
            "cost_currency": "NZD",
        }

        # Generate external statistics for Energy Dashboard
        await self.generate_statistics(billing_periods)

    async def generate_statistics(  # noqa: PLR0915 -- builds four parallel running-sum series in one pass; splitting would obscure the shared iteration
        self, billing_periods: list[dict[str, Any]]
    ) -> None:
        """
        Generate external statistics from billing period data.

        Follows the Energy Dashboard pattern.
        """
        if not billing_periods:
            return

        period_statistics = []
        cost_statistics = []
        consumption_cost_statistics = []
        wastewater_cost_statistics = []
        running_sum = 0
        cost_running_sum = 0
        consumption_cost_running_sum = 0
        wastewater_cost_running_sum = 0

        # Sort periods by date (oldest first) for cumulative calculation
        sorted_periods = sorted(
            billing_periods, key=lambda x: x.get("billingPeriodToDate", "")
        )

        for period in sorted_periods:
            end_date_str = period.get("billingPeriodToDate")
            if end_date_str:
                try:
                    # Parse and convert to NZ timezone
                    end_date = datetime.strptime(
                        end_date_str, "%Y-%m-%dT%H:%M:%S.%fZ"
                    ).replace(tzinfo=UTC)
                    end_date = end_date.astimezone(NZ_TIMEZONE)

                    period_usage = period.get("waterUsage", 0)
                    running_sum += period_usage

                    number_of_days = (
                        datetime.strptime(
                            period.get("billingPeriodToDate"), "%Y-%m-%dT%H:%M:%S.%fZ"
                        ).replace(tzinfo=UTC)
                        - datetime.strptime(
                            period.get("billingPeriodFromDate"),
                            "%Y-%m-%dT%H:%M:%S.%fZ",
                        ).replace(tzinfo=UTC)
                    ).days + 1

                    cost_breakdown = self._calculate_cost(period_usage, number_of_days)
                    cost_running_sum += cost_breakdown["total"]
                    consumption_cost_running_sum += cost_breakdown["consumption"]
                    wastewater_cost_running_sum += cost_breakdown["wastewater"]

                    # Running sum is required by the Energy Dashboard
                    period_statistics.append(
                        StatisticData(start=end_date, sum=running_sum)
                    )

                    # Create cost statistics
                    cost_statistics.append(
                        StatisticData(start=end_date, sum=cost_running_sum)
                    )

                    # Create consumption cost statistics if rate is configured
                    if self._consumption_rate > 0:
                        consumption_cost_statistics.append(
                            StatisticData(
                                start=end_date, sum=consumption_cost_running_sum
                            )
                        )

                    # Create wastewater cost statistics if rate is configured
                    if self._wastewater_rate > 0:
                        wastewater_cost_statistics.append(
                            StatisticData(
                                start=end_date, sum=wastewater_cost_running_sum
                            )
                        )

                except (ValueError, TypeError) as e:
                    _LOGGER.warning("Failed to parse date %s: %s", end_date_str, e)
                    continue

        if period_statistics:
            metadata = StatisticMetaData(
                has_sum=True,
                name="Watercare Water Consumption",
                source=DOMAIN,
                statistic_id=f"{DOMAIN}:water_consumption",
                unit_of_measurement=self._unit_of_measurement,
                mean_type=StatisticMeanType.NONE,
                unit_class=None,
            )

            _LOGGER.debug(
                "Adding %s water consumption statistics", len(period_statistics)
            )
            async_add_external_statistics(self.hass, metadata, period_statistics)
        else:
            _LOGGER.warning("No valid consumption statistics generated")

        if cost_statistics:
            cost_metadata = StatisticMetaData(
                has_sum=True,
                name="Watercare Total Cost",
                source=DOMAIN,
                statistic_id=f"{DOMAIN}:water_cost",
                unit_of_measurement="NZD",
                mean_type=StatisticMeanType.NONE,
                unit_class=None,
            )

            _LOGGER.debug("Adding %s water cost statistics", len(cost_statistics))
            async_add_external_statistics(self.hass, cost_metadata, cost_statistics)
        else:
            _LOGGER.warning("No valid cost statistics generated")

        # Add consumption cost statistics if configured
        if consumption_cost_statistics and self._consumption_rate > 0:
            consumption_cost_metadata = StatisticMetaData(
                has_sum=True,
                name="Watercare Consumption Cost",
                source=DOMAIN,
                statistic_id=f"{DOMAIN}:consumption_cost",
                unit_of_measurement="NZD",
                mean_type=StatisticMeanType.NONE,
                unit_class=None,
            )

            _LOGGER.debug(
                "Adding %s consumption cost statistics",
                len(consumption_cost_statistics),
            )
            async_add_external_statistics(
                self.hass, consumption_cost_metadata, consumption_cost_statistics
            )

        # Add wastewater cost statistics if configured
        if wastewater_cost_statistics and self._wastewater_rate > 0:
            wastewater_cost_metadata = StatisticMetaData(
                has_sum=True,
                name="Watercare Wastewater Cost",
                source=DOMAIN,
                statistic_id=f"{DOMAIN}:wastewater_cost",
                unit_of_measurement="NZD",
                mean_type=StatisticMeanType.NONE,
                unit_class=None,
            )

            _LOGGER.debug(
                "Adding %s wastewater cost statistics",
                len(wastewater_cost_statistics),
            )
            async_add_external_statistics(
                self.hass, wastewater_cost_metadata, wastewater_cost_statistics
            )

    async def process_daily_data(  # noqa: PLR0915 -- builds four parallel running-sum series in one pass; splitting would obscure the shared iteration
        self, response: str | None
    ) -> None:
        """Process the daily data."""
        if response is None:
            _LOGGER.error(
                "No response received from Watercare API; skipping processing"
            )
            return

        try:
            parsed_data = json.loads(response)
        except json.JSONDecodeError:
            _LOGGER.exception(
                "Failed to parse JSON response for dailywithstats endpoint"
            )
            return

        _LOGGER.debug("Parsed data: %s", parsed_data)
        usage_data = parsed_data.get("usage", [])
        statistic_data = parsed_data.get("statistics", {})

        litres_running_sum = 0
        daily_consumption = {}

        for entry in usage_data:
            timestamp_str = entry.get("timestamp")
            litres = entry.get("litres", 0)
            timestamp = datetime.strptime(
                timestamp_str, "%Y-%m-%dT%H:%M:%S.%fZ"
            ).replace(tzinfo=UTC)
            timestamp = timestamp.astimezone(NZ_TIMEZONE)
            date_str = timestamp.strftime("%Y-%m-%d")

            daily_consumption[date_str] = daily_consumption.get(date_str, 0) + litres

        _LOGGER.debug("Daily consumption: %s", daily_consumption)

        # Assign yesterday's consumption to state
        yesterday_date = (datetime.now(NZ_TIMEZONE) - timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )
        yesterday_consumption = daily_consumption.get(yesterday_date, 0)
        self._state = yesterday_consumption
        _LOGGER.debug("yesterday_consumption: %s", yesterday_consumption)

        # Calculate cost for yesterday's consumption
        cost_breakdown = self._calculate_cost(yesterday_consumption, 1)

        efficiency_data = statistic_data.get("efficiency", {})
        self._state_attributes = {
            "yesterday_consumption": yesterday_consumption,
            "current_period_cost": round(cost_breakdown["total"], 2),
            "current_period_cost_consumption": round(cost_breakdown["consumption"], 2),
            "current_period_cost_wastewater": round(cost_breakdown["wastewater"], 2),
            "consumption_rate_per_1000L": self._consumption_rate,
            "wastewater_rate_per_1000L": self._wastewater_rate,
            "endpoint": self._endpoint,
            "cost_currency": "NZD",
            "account_balance": parsed_data.get("accountBalance"),
            "amount_due": parsed_data.get("amountDue"),
            "reading_type": parsed_data.get("readingType"),
            "currentPeriodAverage": statistic_data.get("currentPeriodAverage"),
            "differenceToPreviousPeriod": statistic_data.get(
                "differenceToPreviousPeriod"
            ),
            "currentHouseholdBand": efficiency_data.get("currentHouseholdBand"),
            "usageToLowerBand": efficiency_data.get("usageToLowerBand"),
        }

        # Generate statistics for daily data
        day_statistics = []
        cost_statistics = []
        consumption_cost_statistics = []
        wastewater_cost_statistics = []
        running_cost_sum = 0
        consumption_cost_running_sum = 0
        wastewater_cost_running_sum = 0
        first = True

        for date, litres in daily_consumption.items():
            start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=NZ_TIMEZONE)

            # HASSIO statistics require values to be a running sum of all
            # previous values.
            litres_running_sum += litres

            # Calculate cost for this day
            daily_cost_breakdown = self._calculate_cost(litres, 1)
            running_cost_sum += daily_cost_breakdown["total"]
            consumption_cost_running_sum += daily_cost_breakdown["consumption"]
            wastewater_cost_running_sum += daily_cost_breakdown["wastewater"]

            if first:
                reset = start
                first = False

            # Add consumption statistics
            day_statistics.append(
                StatisticData(start=start, sum=litres_running_sum, last_reset=reset)
            )

            # Add cost statistics
            cost_statistics.append(StatisticData(start=start, sum=running_cost_sum))

            # Add consumption cost statistics if rate is configured
            if self._consumption_rate > 0:
                consumption_cost_statistics.append(
                    StatisticData(start=start, sum=consumption_cost_running_sum)
                )

            # Add wastewater cost statistics if rate is configured
            if self._wastewater_rate > 0:
                wastewater_cost_statistics.append(
                    StatisticData(start=start, sum=wastewater_cost_running_sum)
                )

        # Add daily consumption statistics
        if day_statistics:
            day_metadata = StatisticMetaData(
                has_sum=True,
                name=self._get_statistic_name("consumption"),
                source=DOMAIN,
                statistic_id=f"{DOMAIN}:daily_consumption",
                unit_of_measurement=self._unit_of_measurement,
                mean_type=StatisticMeanType.NONE,
                unit_class=None,
            )

            _LOGGER.debug("Adding %s daily consumption statistics", len(day_statistics))
            async_add_external_statistics(self.hass, day_metadata, day_statistics)
        else:
            _LOGGER.warning("No daily statistics found, skipping update")

        # Add daily cost statistics
        if cost_statistics:
            cost_metadata = StatisticMetaData(
                has_sum=True,
                name="Watercare Daily Cost",
                source=DOMAIN,
                statistic_id=f"{DOMAIN}:daily_cost",
                unit_of_measurement="NZD",
                mean_type=StatisticMeanType.NONE,
                unit_class=None,
            )

            _LOGGER.debug("Adding %s daily cost statistics", len(cost_statistics))
            async_add_external_statistics(self.hass, cost_metadata, cost_statistics)

        # Add daily consumption cost statistics if configured
        if consumption_cost_statistics and self._consumption_rate > 0:
            consumption_cost_metadata = StatisticMetaData(
                has_sum=True,
                name="Watercare Daily Consumption Cost",
                source=DOMAIN,
                statistic_id=f"{DOMAIN}:daily_consumption_cost",
                unit_of_measurement="NZD",
                mean_type=StatisticMeanType.NONE,
                unit_class=None,
            )

            _LOGGER.debug(
                "Adding %s daily consumption cost statistics",
                len(consumption_cost_statistics),
            )
            async_add_external_statistics(
                self.hass, consumption_cost_metadata, consumption_cost_statistics
            )

        # Add daily wastewater cost statistics if configured
        if wastewater_cost_statistics and self._wastewater_rate > 0:
            wastewater_cost_metadata = StatisticMetaData(
                has_sum=True,
                name="Watercare Daily Wastewater Cost",
                source=DOMAIN,
                statistic_id=f"{DOMAIN}:daily_wastewater_cost",
                unit_of_measurement="NZD",
                mean_type=StatisticMeanType.NONE,
                unit_class=None,
            )

            _LOGGER.debug(
                "Adding %s daily wastewater cost statistics",
                len(wastewater_cost_statistics),
            )
            async_add_external_statistics(
                self.hass, wastewater_cost_metadata, wastewater_cost_statistics
            )
