"""Watercare sensors."""

from __future__ import annotations

import calendar
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData
from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.components.sensor import SensorEntity
from homeassistant.util.unit_conversion import VolumeConverter

from .const import (
    CONF_ANNUAL_LINE_CHARGE,
    CONF_CONSUMPTION_RATE,
    CONF_ENDPOINT,
    CONF_HISTORY_BACKFILLED,
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
from .statistics_helpers import accumulate_cost_series, push_statistic_series

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .api import WatercareApi

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(hours=12)

# The halfhourly endpoint silently caps each request at ~161 days regardless
# of the requested range (it always starts exactly at the requested `from`
# date) -- validated empirically against the live API on 2026-08-05.
HALFHOURLY_BACKFILL_MAX_DAYS = 1095  # ~3 years
HALFHOURLY_BACKFILL_CHUNK_DAYS = 150  # safety margin under the ~161-day cap

# dailywithstats and monthly also have no server-side default date range and
# 400 (dailywithstats: INVALID_REQUEST_BODY, monthly: INVALID_REQUEST_QUERY)
# without explicit from/to -- unlike mechanicalmonthly, which has its own
# default -- validated empirically against the live API on 2026-08-08.
# Unlike halfhourly, a single request spanning this whole window isn't
# capped -- it returns full account history in one response -- and their
# processing recomputes cumulative sums from the full response on every
# poll (no incremental watermark like halfhourly's), so request the full
# span every time rather than a short rolling window.
FULL_HISTORY_LOOKBACK_DAYS = 1095


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> bool:
    """Set up the Watercare sensor platform."""
    entry_data = hass.data[DOMAIN].get(entry.entry_id, {})
    if "api" not in entry_data:
        _LOGGER.error("API instance not found in config entry data.")
        return False

    api = entry_data["api"]

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

    sensor = WatercareUsageSensor(
        SENSOR_NAME,
        api,
        consumption_rate,
        wastewater_rate,
        wastewater_ratio,
        annual_line_charge,
        endpoint,
    )
    entry_data["sensor"] = sensor

    needs_backfill = endpoint == "halfhourly" and not entry.data.get(
        CONF_HISTORY_BACKFILLED
    )

    # When a backfill is pending, skip the immediate update-before-add: the
    # backfill must run and land its (older) history first, since
    # _async_push_hourly_statistic only appends points after the latest
    # already-stored one -- doing the regular update first would set that
    # watermark to "now" and cause the backfill's data to be silently
    # discarded as already-superseded.
    async_add_entities([sensor], update_before_add=not needs_backfill)

    if needs_backfill:

        async def _run_backfill() -> None:
            await sensor.async_backfill_halfhourly_history()
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_HISTORY_BACKFILLED: True}
            )
            await sensor.async_update()
            sensor.async_write_ha_state()

        entry.async_create_background_task(
            hass, _run_backfill(), "watercare_history_backfill"
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
        # halfhourly's entity state is "today's consumption so far" and
        # monthly's is "this month's consumption so far" -- both reset
        # within a single sensor lifetime, which total_increasing forbids,
        # so use measurement instead. dailywithstats/mechanicalmonthly's
        # states are previous-day/billing-period totals that don't reset
        # the same way.
        self._state_class = (
            "measurement"
            if endpoint in ("halfhourly", "monthly")
            else "total_increasing"
        )
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
        self, usage_litres: float, number_of_days: float
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
            "line_charge": line_charge,
        }

    def _parse_json_response(
        self,
        response: str | None,
        *,
        exceptions: tuple[type[Exception], ...] = (TypeError, json.JSONDecodeError),
        error_message: str = "Failed to parse Watercare API response",
    ) -> Any | None:
        """Parse a JSON API response, logging and returning None on failure."""
        if response is None:
            _LOGGER.error(
                "No response received from Watercare API; skipping processing"
            )
            return None
        try:
            return json.loads(response)
        except exceptions:
            _LOGGER.exception(error_message)
            return None

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

        start_date = None
        end_date = None
        if self._endpoint == "halfhourly":
            # Unlike the other endpoints, halfhourly has no server-side
            # default range and 400s (INVALID_REQUEST_BODY) without one.
            today = datetime.now(NZ_TIMEZONE).date()
            start_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")
            end_date = today.strftime("%Y-%m-%d")
        elif self._endpoint in ("dailywithstats", "monthly"):
            # These also have no server-side default range and 400 without
            # one, but (unlike halfhourly) aren't capped by request span and
            # their processing recomputes cumulative sums from the full
            # response each poll, so request the full history span.
            today = datetime.now(NZ_TIMEZONE).date()
            start_date = (today - timedelta(days=FULL_HISTORY_LOOKBACK_DAYS)).strftime(
                "%Y-%m-%d"
            )
            end_date = today.strftime("%Y-%m-%d")

        response = await self._api.get_data(
            endpoint=self._endpoint, start_date=start_date, end_date=end_date
        )

        # Route to appropriate processing method based on endpoint
        if self._endpoint == "dailywithstats":
            await self.process_daily_data(response)
        elif self._endpoint == "halfhourly":
            # halfhourly returns a bare list of {timestamp, litres} readings,
            # not billing periods or a dailywithstats-style wrapper.
            await self.process_halfhourly_data(response)
        elif self._endpoint == "monthly":
            # monthly returns one entry per calendar month --
            # {timestamp, litres, numberOfMissingDays, statistics} -- not
            # billing periods, so it needs its own processing too.
            await self.process_monthly_data(response)
        else:
            # mechanicalmonthly has its own server-side default range and
            # returns real billing periods.
            await self.process_data(response)

    async def process_data(self, response: str | None) -> None:
        """Process the API response."""
        billing_periods = self._parse_json_response(response)
        if billing_periods is None:
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

    async def generate_statistics(self, billing_periods: list[dict[str, Any]]) -> None:
        """
        Generate external statistics from billing period data.

        Follows the Energy Dashboard pattern.
        """
        if not billing_periods:
            return

        # Sort periods by date (oldest first) for cumulative calculation
        sorted_periods = sorted(
            billing_periods, key=lambda x: x.get("billingPeriodToDate", "")
        )

        entries = []
        for period in sorted_periods:
            end_date_str = period.get("billingPeriodToDate")
            if not end_date_str:
                continue
            try:
                # Parse and convert to NZ timezone
                end_date = datetime.strptime(
                    end_date_str, "%Y-%m-%dT%H:%M:%S.%fZ"
                ).replace(tzinfo=UTC)
                end_date = end_date.astimezone(NZ_TIMEZONE)

                number_of_days = (
                    datetime.strptime(end_date_str, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                        tzinfo=UTC
                    )
                    - datetime.strptime(
                        period.get("billingPeriodFromDate"),
                        "%Y-%m-%dT%H:%M:%S.%fZ",
                    ).replace(tzinfo=UTC)
                ).days + 1
            except (ValueError, TypeError) as e:
                _LOGGER.warning("Failed to parse date %s: %s", end_date_str, e)
                continue

            entries.append((end_date, period.get("waterUsage", 0), number_of_days))

        (
            consumption_points,
            cost_points,
            consumption_cost_points,
            wastewater_cost_points,
        ) = accumulate_cost_series(
            entries,
            calculate_cost=self._calculate_cost,
            consumption_rate=self._consumption_rate,
            wastewater_rate=self._wastewater_rate,
        )

        push_statistic_series(
            self.hass,
            consumption_points,
            statistic_id=f"{DOMAIN}:water_consumption",
            name="Watercare Water Consumption",
            unit=self._unit_of_measurement,
            unit_class=VolumeConverter.UNIT_CLASS,
            empty_warning="No valid consumption statistics generated",
        )
        push_statistic_series(
            self.hass,
            cost_points,
            statistic_id=f"{DOMAIN}:water_cost",
            name="Watercare Total Cost",
            unit="NZD",
            unit_class=None,
            empty_warning="No valid cost statistics generated",
        )
        push_statistic_series(
            self.hass,
            consumption_cost_points,
            statistic_id=f"{DOMAIN}:consumption_cost",
            name="Watercare Consumption Cost",
            unit="NZD",
            unit_class=None,
            empty_warning=None,
        )
        push_statistic_series(
            self.hass,
            wastewater_cost_points,
            statistic_id=f"{DOMAIN}:wastewater_cost",
            name="Watercare Wastewater Cost",
            unit="NZD",
            unit_class=None,
            empty_warning=None,
        )

    def _days_in_monthly_reading(self, reading: dict[str, Any]) -> int:
        """Estimate the number of days a monthly reading's litres figure covers."""
        timestamp_str = reading.get("timestamp")
        try:
            ts = datetime.strptime(timestamp_str, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                tzinfo=UTC
            )
        except (
            TypeError,
            ValueError,
        ):
            return 30
        days_in_month = calendar.monthrange(ts.year, ts.month)[1]
        missing_days = reading.get("numberOfMissingDays", 0) or 0
        return max(days_in_month - missing_days, 1)

    async def process_monthly_data(self, response: str | None) -> None:
        """
        Process the monthly API response.

        Unlike `mechanicalmonthly`, `monthly` returns one entry per calendar
        month -- {timestamp, litres, numberOfMissingDays, statistics} --
        not billing periods, so it needs its own running-sum handling.
        """
        monthly_readings = self._parse_json_response(response)
        if monthly_readings is None:
            return

        _LOGGER.debug(
            "Processing monthly data: %s entries", len(monthly_readings or [])
        )

        if not monthly_readings:
            _LOGGER.warning("No monthly readings found")
            return

        sorted_readings = sorted(monthly_readings, key=lambda x: x.get("timestamp", ""))

        latest = sorted_readings[-1]
        latest_usage = latest.get("litres", 0)
        self._state = latest_usage

        statistics = latest.get("statistics", {})
        efficiency = statistics.get("efficiency", {})
        cost_breakdown = self._calculate_cost(
            latest_usage, self._days_in_monthly_reading(latest)
        )
        self._state_attributes = {
            "month_usage": latest_usage,
            "month_ending": latest.get("timestamp"),
            "number_of_missing_days": latest.get("numberOfMissingDays"),
            "current_period_average": statistics.get("currentPeriodAverage"),
            "difference_to_previous_period": statistics.get(
                "differenceToPreviousPeriod"
            ),
            "household_efficiency_band": efficiency.get("currentHouseholdBand"),
            "usage_to_lower_band": efficiency.get("usageToLowerBand"),
            "current_period_cost": round(cost_breakdown["total"], 2),
            "current_period_cost_consumption": round(cost_breakdown["consumption"], 2),
            "current_period_cost_wastewater": round(cost_breakdown["wastewater"], 2),
            "consumption_rate_per_1000L": self._consumption_rate,
            "wastewater_rate_per_1000L": self._wastewater_rate,
            "endpoint": self._endpoint,
            "cost_currency": "NZD",
        }

        await self.generate_monthly_statistics(sorted_readings)

    async def generate_monthly_statistics(
        self, monthly_readings: list[dict[str, Any]]
    ) -> None:
        """
        Generate external statistics from monthly reading data.

        Follows the same running-sum pattern as generate_statistics, but
        keyed by calendar month instead of billing period.
        """
        if not monthly_readings:
            return

        entries = []
        for reading in monthly_readings:
            timestamp_str = reading.get("timestamp")
            if not timestamp_str:
                continue
            try:
                end_date = datetime.strptime(
                    timestamp_str, "%Y-%m-%dT%H:%M:%S.%fZ"
                ).replace(tzinfo=UTC)
            except ValueError as e:
                _LOGGER.warning("Failed to parse date %s: %s", timestamp_str, e)
                continue
            end_date = end_date.astimezone(NZ_TIMEZONE)

            entries.append(
                (
                    end_date,
                    reading.get("litres", 0),
                    self._days_in_monthly_reading(reading),
                )
            )

        (
            consumption_points,
            cost_points,
            consumption_cost_points,
            wastewater_cost_points,
        ) = accumulate_cost_series(
            entries,
            calculate_cost=self._calculate_cost,
            consumption_rate=self._consumption_rate,
            wastewater_rate=self._wastewater_rate,
        )

        push_statistic_series(
            self.hass,
            consumption_points,
            statistic_id=f"{DOMAIN}:monthly_consumption",
            name=self._get_statistic_name("consumption"),
            unit=self._unit_of_measurement,
            unit_class=VolumeConverter.UNIT_CLASS,
            empty_warning="No valid monthly consumption statistics generated",
        )
        push_statistic_series(
            self.hass,
            cost_points,
            statistic_id=f"{DOMAIN}:monthly_cost",
            name="Watercare Monthly Cost",
            unit="NZD",
            unit_class=None,
            empty_warning="No valid monthly cost statistics generated",
        )
        push_statistic_series(
            self.hass,
            consumption_cost_points,
            statistic_id=f"{DOMAIN}:monthly_consumption_cost",
            name="Watercare Monthly Consumption Cost",
            unit="NZD",
            unit_class=None,
            empty_warning=None,
        )
        push_statistic_series(
            self.hass,
            wastewater_cost_points,
            statistic_id=f"{DOMAIN}:monthly_wastewater_cost",
            name="Watercare Monthly Wastewater Cost",
            unit="NZD",
            unit_class=None,
            empty_warning=None,
        )

    def _bucket_hourly_readings(
        self, readings: list[dict[str, Any]]
    ) -> dict[datetime, float]:
        """Bucket a list of {timestamp, litres} readings into hourly sums."""
        hourly_consumption: dict[datetime, float] = {}
        for reading in readings:
            timestamp_str = reading.get("timestamp")
            if not timestamp_str:
                continue
            try:
                reading_time = datetime.strptime(
                    timestamp_str, "%Y-%m-%dT%H:%M:%S.%fZ"
                ).replace(tzinfo=UTC)
            except ValueError:
                _LOGGER.warning("Failed to parse timestamp %s", timestamp_str)
                continue
            reading_time = reading_time.astimezone(NZ_TIMEZONE)
            hour_start = reading_time.replace(minute=0, second=0, microsecond=0)
            litres = reading.get("litres", 0)
            hourly_consumption[hour_start] = (
                hourly_consumption.get(hour_start, 0) + litres
            )
        return hourly_consumption

    async def _async_last_statistic_sum(
        self, statistic_id: str
    ) -> tuple[float, float | None]:
        """
        Return (last cumulative sum, last start timestamp) for a statistic.

        Returns (0.0, None) if the statistic doesn't exist yet.
        """
        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            statistic_id,
            True,  # noqa: FBT003 -- positional per get_last_statistics' signature
            {"sum"},
        )
        records = last_stat.get(statistic_id)
        if not records:
            return 0.0, None
        return float(records[0].get("sum") or 0.0), records[0]["start"]

    async def _async_push_hourly_statistic(  # noqa: PLR0913 -- one series definition per call keeps push callers declarative; bundling into a dataclass would ripple with no behavior change
        self,
        hourly_consumption: dict[datetime, float],
        *,
        statistic_id: str,
        name: str,
        unit: str,
        cost_key: str | None,
        rate_gate: float | None,
        unit_class: str | None = None,
    ) -> int:
        """
        Push one incremental hourly external-statistics series.

        Continues from whatever cumulative sum is already stored for
        `statistic_id` rather than resetting to zero, and only appends
        hours after the last stored one -- so repeated polls and a one-off
        deep backfill can safely share the same statistic history without
        the running total jumping backwards.

        Returns the number of new points actually written -- callers that
        expect to backfill older history should check this, since it will
        be 0 if `hourly_consumption` is entirely at or before the existing
        watermark.
        """
        if rate_gate is not None and rate_gate <= 0:
            return 0

        running_sum, last_start = await self._async_last_statistic_sum(statistic_id)

        new_points = []
        for hour_start in sorted(hourly_consumption):
            if last_start is not None and hour_start.timestamp() <= last_start:
                continue
            litres = hourly_consumption[hour_start]
            if cost_key is None:
                running_sum += litres
            else:
                running_sum += self._calculate_cost(litres, 1 / 24)[cost_key]
            new_points.append(StatisticData(start=hour_start, sum=running_sum))

        push_statistic_series(
            self.hass,
            new_points,
            statistic_id=statistic_id,
            name=name,
            unit=unit,
            unit_class=unit_class,
            empty_warning=None,
        )
        return len(new_points)

    async def _async_push_halfhourly_statistics(
        self, hourly_consumption: dict[datetime, float]
    ) -> int:
        """
        Push all four hourly statistic series for the halfhourly endpoint.

        Returns the number of new consumption points written (the other
        three series share the same watermark and will match, unless
        gated off entirely by a zero rate).
        """
        new_count = await self._async_push_hourly_statistic(
            hourly_consumption,
            statistic_id=f"{DOMAIN}:halfhourly_consumption",
            name=self._get_statistic_name("consumption"),
            unit=self._unit_of_measurement,
            cost_key=None,
            rate_gate=None,
            unit_class=VolumeConverter.UNIT_CLASS,
        )
        await self._async_push_hourly_statistic(
            hourly_consumption,
            statistic_id=f"{DOMAIN}:halfhourly_cost",
            name="Watercare Half-hourly Cost",
            unit="NZD",
            cost_key="total",
            rate_gate=None,
        )
        await self._async_push_hourly_statistic(
            hourly_consumption,
            statistic_id=f"{DOMAIN}:halfhourly_consumption_cost",
            name="Watercare Half-hourly Consumption Cost",
            unit="NZD",
            cost_key="consumption",
            rate_gate=self._consumption_rate,
        )
        await self._async_push_hourly_statistic(
            hourly_consumption,
            statistic_id=f"{DOMAIN}:halfhourly_wastewater_cost",
            name="Watercare Half-hourly Wastewater Cost",
            unit="NZD",
            cost_key="wastewater",
            rate_gate=self._wastewater_rate,
        )
        return new_count

    async def process_halfhourly_data(self, response: str | None) -> None:
        """
        Process the halfhourly API response.

        Unlike the other endpoints, halfhourly returns a bare list of
        {timestamp, litres} readings with no billing/account metadata.
        """
        readings = self._parse_json_response(response)
        if readings is None:
            return

        _LOGGER.debug("Processing halfhourly data: %s readings", len(readings or []))

        if not readings:
            _LOGGER.warning("No halfhourly readings found")
            return

        hourly_consumption = self._bucket_hourly_readings(readings)
        if not hourly_consumption:
            _LOGGER.warning("No valid halfhourly readings found")
            return

        # Today's consumption so far, for the sensor's own state/attributes.
        today = datetime.now(NZ_TIMEZONE).date()
        today_consumption = sum(
            litres
            for hour, litres in hourly_consumption.items()
            if hour.date() == today
        )
        self._state = today_consumption

        cost_breakdown = self._calculate_cost(today_consumption, 1)
        self._state_attributes = {
            "today_consumption": today_consumption,
            "current_period_cost": round(cost_breakdown["total"], 2),
            "current_period_cost_consumption": round(cost_breakdown["consumption"], 2),
            "current_period_cost_wastewater": round(cost_breakdown["wastewater"], 2),
            "consumption_rate_per_1000L": self._consumption_rate,
            "wastewater_rate_per_1000L": self._wastewater_rate,
            "endpoint": self._endpoint,
            "cost_currency": "NZD",
        }

        await self._async_push_halfhourly_statistics(hourly_consumption)

    async def async_backfill_halfhourly_history(self) -> None:
        """
        One-time deep import of halfhourly history beyond the normal rolling window.

        The API silently caps each request at ~161 days regardless of the
        requested range (it always starts exactly at the requested `from`
        date), so this pages backward in fixed-size chunks.
        """
        _LOGGER.info("Starting Watercare halfhourly history backfill")
        today = datetime.now(NZ_TIMEZONE).date()
        chunk_end = today
        all_readings: dict[str, float] = {}

        while (today - chunk_end).days < HALFHOURLY_BACKFILL_MAX_DAYS:
            chunk_start = chunk_end - timedelta(days=HALFHOURLY_BACKFILL_CHUNK_DAYS)
            response = await self._api.get_data(
                endpoint="halfhourly",
                start_date=chunk_start.strftime("%Y-%m-%d"),
                end_date=chunk_end.strftime("%Y-%m-%d"),
            )
            if response is None:
                _LOGGER.warning(
                    "Backfill aborted: request for %s to %s failed; "
                    "no statistics written so a retry can resume from the start",
                    chunk_start,
                    chunk_end,
                )
                return

            try:
                readings = json.loads(response)
            except (
                TypeError,
                json.JSONDecodeError,
            ):
                _LOGGER.exception(
                    "Backfill aborted: failed to parse response for %s to %s; "
                    "no statistics written so a retry can resume from the start",
                    chunk_start,
                    chunk_end,
                )
                return

            if not readings:
                _LOGGER.info(
                    "Backfill reached the start of available history at %s", chunk_end
                )
                break

            for reading in readings:
                timestamp_str = reading.get("timestamp")
                if timestamp_str:
                    all_readings[timestamp_str] = reading.get("litres", 0)

            chunk_end = chunk_start

        if not all_readings:
            _LOGGER.warning("Backfill found no historical data")
            return

        hourly_consumption = self._bucket_hourly_readings(
            [
                {"timestamp": timestamp, "litres": litres}
                for timestamp, litres in all_readings.items()
            ]
        )

        # Pushing is append-only (see _async_push_hourly_statistic): it only
        # writes hours after the statistic's existing watermark. If regular
        # polling has already run and advanced that watermark, none of this
        # deep-history data -- which is by definition older -- can actually
        # be written, even though the API calls above succeeded.
        _, last_start = await self._async_last_statistic_sum(
            f"{DOMAIN}:halfhourly_consumption"
        )
        oldest_collected = min(hourly_consumption)
        if last_start is not None and oldest_collected.timestamp() <= last_start:
            _LOGGER.warning(
                "Backfill collected %s hourly buckets back to %s, but existing "
                "halfhourly statistics already start at or before that point "
                "(from regular polling) -- history is only ever appended after "
                "the latest stored hour, so this older data won't be written. "
                "To re-import deeper history, clear the watercare:halfhourly_* "
                "statistics first (Developer tools > Statistics) and re-run "
                "this service",
                len(hourly_consumption),
                oldest_collected,
            )

        new_count = await self._async_push_halfhourly_statistics(hourly_consumption)
        if new_count:
            _LOGGER.info(
                "Backfill complete: wrote %s new hourly buckets (of %s collected)",
                new_count,
                len(hourly_consumption),
            )
        else:
            _LOGGER.warning(
                "Backfill collected %s hourly buckets but wrote none -- they "
                "were all at or before the existing statistics watermark",
                len(hourly_consumption),
            )

    async def process_daily_data(self, response: str | None) -> None:
        """Process the daily data."""
        parsed_data = self._parse_json_response(
            response,
            exceptions=(json.JSONDecodeError,),
            error_message="Failed to parse JSON response for dailywithstats endpoint",
        )
        if parsed_data is None:
            return

        _LOGGER.debug("Parsed data: %s", parsed_data)
        usage_data = parsed_data.get("usage", [])
        statistic_data = parsed_data.get("statistics", {})

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
        entries = []
        reset = None
        for date, litres in daily_consumption.items():
            start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=NZ_TIMEZONE)
            if reset is None:
                reset = start
            entries.append((start, litres, 1))

        (
            consumption_points,
            cost_points,
            consumption_cost_points,
            wastewater_cost_points,
        ) = accumulate_cost_series(
            entries,
            calculate_cost=self._calculate_cost,
            consumption_rate=self._consumption_rate,
            wastewater_rate=self._wastewater_rate,
            last_reset=reset,
        )

        push_statistic_series(
            self.hass,
            consumption_points,
            statistic_id=f"{DOMAIN}:daily_consumption",
            name=self._get_statistic_name("consumption"),
            unit=self._unit_of_measurement,
            unit_class=VolumeConverter.UNIT_CLASS,
            empty_warning="No daily statistics found, skipping update",
        )
        push_statistic_series(
            self.hass,
            cost_points,
            statistic_id=f"{DOMAIN}:daily_cost",
            name="Watercare Daily Cost",
            unit="NZD",
            unit_class=None,
            empty_warning=None,
        )
        push_statistic_series(
            self.hass,
            consumption_cost_points,
            statistic_id=f"{DOMAIN}:daily_consumption_cost",
            name="Watercare Daily Consumption Cost",
            unit="NZD",
            unit_class=None,
            empty_warning=None,
        )
        push_statistic_series(
            self.hass,
            wastewater_cost_points,
            statistic_id=f"{DOMAIN}:daily_wastewater_cost",
            name="Watercare Daily Wastewater Cost",
            unit="NZD",
            unit_class=None,
            empty_warning=None,
        )
