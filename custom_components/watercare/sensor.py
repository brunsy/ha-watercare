"""Watercare sensors."""

from datetime import datetime, timedelta
import asyncio
import logging
import json
import pytz

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.components.sensor import PLATFORM_SCHEMA, SensorEntity
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics

from .api import WatercareApi
from .const import DOMAIN, SENSOR_NAME

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(hours=12)

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback, discovery_info=None
):
    """Set up the Watercare sensor platform."""
    
    if "api" not in hass.data[DOMAIN]:
        _LOGGER.error("API instance not found in config entry data.")
        return False

    api = hass.data[DOMAIN]["api"]
    async_add_entities([WatercareUsageSensor(SENSOR_NAME, api)], True)

class WatercareUsageSensor(SensorEntity):
    """Define Watercare Usage sensor."""

    def __init__(self, name, api):
        """Initialize Watercare Usage sensor."""
        self._name = name
        self._icon = "mdi:water"
        self._state = None
        self._unit_of_measurement = "L"
        self._unique_id = DOMAIN
        self._device_class = "water"
        self._state_class = "total"
        self._state_attributes = {}
        self._api = api

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def icon(self):
        """Icon to use in the frontend, if any."""
        return self._icon

    @property
    def state(self):
        """Return the state of the device."""
        return self._state

    @property
    def extra_state_attributes(self):
        """Return the state attributes of the sensor."""
        return self._state_attributes

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        return self._unit_of_measurement

    @property
    def state_class(self):
        """Return the state class."""
        return self._state_class

    @property
    def device_class(self):
        """Return the device class."""
        return self._device_class

    @property
    def unique_id(self):
        """Return the unique id."""
        return self._unique_id

    async def async_update(self):
        """Update the sensor data."""
        _LOGGER.debug("Beginning sensor update")
        response = await self._api.get_data(endpoint="halfhourly")
        await self.process_data(response)


    async def process_data(self, response):
        """Process the half hourly data."""
        parsed_data = json.loads(response)
        _LOGGER.debug(f"Parsed data: {parsed_data}")
        usage_data = parsed_data

        litresRunningSum = 0
        daily_consumption = {}
		
        hourly_consumption = {}
        
		# HASSIO stats needs hourly data not half hourly
        for entry in usage_data:
            timestamp_str = entry.get("timestamp")
            litres = entry.get("litres", 0)

            hourly_timestamp = datetime.strptime(timestamp_str, "%Y-%m-%dT%H:%M:%S.%fz").replace(minute=0, second=0, microsecond=0).replace(tzinfo=pytz.utc)
            hourly_timestamp_str = hourly_timestamp.strftime("%Y-%m-%dT%H")
            hourly_consumption.setdefault(hourly_timestamp_str, {'litres': 0, 'hourly_timestamp': hourly_timestamp})
            
            hourly_consumption[hourly_timestamp_str]['litres'] += litres
            hourly_consumption[hourly_timestamp_str]['hourly_timestamp'] = hourly_timestamp

        hour_statistics = []
        first=True
        for hour, data in hourly_consumption.items():
            start = data['hourly_timestamp']
            
			# HASSIO statistics requires us to add values as a sum of all previous values.
            litresRunningSum += data['litres']

            if first:
                reset = start
                first=False

            statistic_data = {
                "start": start,
                "sum": litresRunningSum,
                "last_reset": reset,
            }
            hour_statistics.append(StatisticData(statistic_data))

        sensor_type = "consumption"
        if hour_statistics:
            day_metadata = StatisticMetaData(
                has_mean= False,
                has_sum= True,
                name= f"{DOMAIN} {sensor_type}",
                source= DOMAIN,
                statistic_id= f"{DOMAIN}:{sensor_type}",
                unit_of_measurement= self._unit_of_measurement,
			)

            _LOGGER.debug(f"Day statistics: {hour_statistics}")
            async_add_external_statistics(self.hass, day_metadata, hour_statistics)
        else:
            _LOGGER.warning("No day statistics found, skipping update")


    async def process_daily_data(self, response):
        """Process the daily data."""
        parsed_data = json.loads(response)
        _LOGGER.debug(f"Parsed data: {parsed_data}")
        usage_data = parsed_data.get("usage", [])
        statistic_data = parsed_data.get("statistics", {})

        litresRunningSum = 0
        daily_consumption = {}
        nz_timezone = pytz.timezone("Pacific/Auckland")

        for entry in usage_data:
            timestamp_str = entry.get("timestamp")
            litres = entry.get("litres", 0)
            timestamp = datetime.strptime(timestamp_str, "%Y-%m-%dT%H:%M:%S.%fZ")
            timestamp = pytz.utc.localize(timestamp).astimezone(nz_timezone)
            date_str = timestamp.strftime("%Y-%m-%d")

            daily_consumption[date_str] = daily_consumption.get(date_str, 0) + litres

        _LOGGER.debug(f"Daily consumption: {daily_consumption}")

        # Assign yesterday's consumption to state
        yesterday_date = (datetime.now(nz_timezone) - timedelta(days=1)).strftime('%Y-%m-%d')
        yesterday_consumption = daily_consumption.get(yesterday_date, 0)
        self._state = yesterday_consumption
        _LOGGER.debug(f"yesterday_consumption: {yesterday_consumption}")

        efficiency_data = statistic_data.get('efficiency', {})
        self._state_attributes.update({
            "currentPeriodAverage": statistic_data.get('currentPeriodAverage'),
            "differenceToPreviousPeriod": statistic_data.get('differenceToPreviousPeriod'),
            "currentHouseholdBand": efficiency_data.get('currentHouseholdBand'),
            "usageToLowerBand": efficiency_data.get('usageToLowerBand'),
        })
        
        day_statistics = []
        first=True
        for date, litres in daily_consumption.items():
            start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=nz_timezone)
            
			# HASSIO statistics requires us to add values as a sum of all previous values.
            litresRunningSum += litres

            if first:
                reset = start
                first=False

            statistic_data = {
                "start": start,
                "sum": litresRunningSum,
                "last_reset": reset,
            }
            
            day_statistics.append(StatisticData(statistic_data))

        sensor_type = "consumption_daily"
        if day_statistics:
            day_metadata = StatisticMetaData(
                has_mean= False,
                has_sum= True,
                name= f"{DOMAIN} {sensor_type}",
                source= DOMAIN,
                statistic_id= f"{DOMAIN}:{sensor_type}",
                unit_of_measurement= self._unit_of_measurement,
			)

            _LOGGER.debug(f"Day statistics: {day_statistics}")
            async_add_external_statistics(self.hass, day_metadata, day_statistics)
        else:
            _LOGGER.warning("No day statistics found, skipping update")
