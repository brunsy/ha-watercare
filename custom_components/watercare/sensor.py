"""Watercare sensors."""

from __future__ import annotations

import asyncio
import calendar
import json
import logging
import math
import re
from datetime import UTC, datetime, timedelta
from datetime import date as date_type
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData
from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.start import async_at_started
from homeassistant.util.unit_conversion import VolumeConverter

from .api import WatercareAuthError
from .const import (
    CONF_ACCOUNT_NUMBER,
    CONF_ANNUAL_LINE_CHARGE,
    CONF_CONSUMPTION_RATE,
    CONF_ENDPOINT,
    CONF_HISTORY_BACKFILLED,
    CONF_LEGACY_IDS,
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
    SUBENTRY_TYPE_ACCOUNT,
)
from .statistics_helpers import accumulate_cost_series, push_statistic_series

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from homeassistant.config_entries import ConfigEntry, ConfigSubentry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .api import WatercareApi

_LOGGER = logging.getLogger(__name__)

# Watercare publishes the previous day's final halfhourly readings around
# 18:15-18:45 UTC, on a fixed UTC clock that ignores NZ daylight saving:
# 06:15-06:45 NZST through 2026-09-26, then 07:15-07:45 NZDT once DST
# started on 2026-09-27 (from the recorder's statistics write times, which
# jumped from ~06:30 NZST to 08:00 NZDT -- the first retry slot after the
# publish -- overnight). The same window was first measured against the
# live API 2026-08-17 to 2026-08-20 (one run caught it mid-publish with an
# incomplete last half-hour), but the exact time isn't fixed enough to just
# pick a single margin. Rather than HA's default SCAN_INTERVAL, which polls
# every 12h from whatever moment the entity happened to be added (so could
# easily land both daily polls before Watercare has published), a standing
# quarter-hour tick (see _schedule_polling) checks the UTC time against
# these slots: while the previous NZ day is incomplete (see
# _is_data_fresh), retry every 15 min through POLL_START_HOUR_UTC's own
# hour (to catch the publish close to when it actually lands), then hourly
# for POLL_RETRY_HOURS more hours -- self-correcting if the publish time
# drifts, and no more API calls than needed once the data's in. If a
# complete day still hasn't shown up by then, give up until the next day's
# start hour rather than retrying indefinitely. The whole window
# (18:00-00:00 UTC) falls within a single NZ calendar day in both NZST and
# NZDT, so "the previous NZ day" means the same day throughout it.
POLL_START_HOUR_UTC = 18
QUARTER_HOUR_MINUTES = (0, 15, 30, 45)
POLL_RETRY_HOURS = 6
# (hour, minute) UTC retry slots: quarter-hourly during POLL_START_HOUR_UTC's
# own hour, then hourly on the hour for POLL_RETRY_HOURS more hours
# (wrapping past midnight UTC), the last one included.
POLL_RETRY_SLOTS_UTC = tuple(
    (POLL_START_HOUR_UTC, minute) for minute in QUARTER_HOUR_MINUTES
) + tuple(
    ((POLL_START_HOUR_UTC + offset) % 24, 0)
    for offset in range(1, POLL_RETRY_HOURS + 1)
)
# Endpoints other than halfhourly have no characterized publish time to
# retry against, so they poll on a fixed twice-daily schedule instead --
# preserving the ~12h cadence of the SCAN_INTERVAL polling that this
# fixed-time scheduling replaced.
SECONDARY_POLL_HOUR_UTC = (POLL_START_HOUR_UTC + 12) % 24

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

# Bounds how long a poll/backfill waits for the recorder to confirm it has
# processed queued statistics writes (see _async_await_recorder_flush) --
# long enough for a normal queue drain, short enough that a stalled/dead
# recorder delays one update cycle rather than wedging _update_lock, and
# therefore every future poll, forever.
_RECORDER_FLUSH_TIMEOUT = 30

# The halfhourly endpoint's regular-poll rolling fetch window (see
# async_update) -- how many days back of readings each poll requests.
HALFHOURLY_POLL_WINDOW_DAYS = 7

# How many of the most recent hourly statistics to fetch in one query when
# looking for the anchor sum just before a healing window starts (see
# _async_statistics_window). Derived from HALFHOURLY_POLL_WINDOW_DAYS (with
# a safety margin) rather than a bare number, so the two can't silently
# drift apart -- if this ever ends up smaller than the poll window, the
# anchor lookup can miss and silently fall back to a 0.0 anchor (see
# _async_statistics_window's docstring), which looks like a meter reset.
_HEAL_LOOKBACK_MARGIN_DAYS = 3
_HEAL_LOOKBACK_STATS = 24 * (HALFHOURLY_POLL_WINDOW_DAYS + _HEAL_LOOKBACK_MARGIN_DAYS)


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
    entry_data["sensors"] = {}

    # One sensor per account subentry, each with its own data source and
    # rates (see config_flow.AccountSubentryFlow).
    for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_ACCOUNT):
        data = subentry.data
        endpoint = data.get(CONF_ENDPOINT, DEFAULT_ENDPOINT)
        sensor = WatercareUsageSensor(
            SENSOR_NAME,
            api,
            data.get(CONF_CONSUMPTION_RATE, DEFAULT_CONSUMPTION_RATE),
            data.get(CONF_WASTEWATER_RATE, DEFAULT_WASTEWATER_RATE),
            data.get(CONF_WASTEWATER_RATIO, DEFAULT_WASTEWATER_RATIO),
            data.get(CONF_ANNUAL_LINE_CHARGE, DEFAULT_ANNUAL_LINE_CHARGE),
            endpoint,
            entry,
            needs_backfill=endpoint == "halfhourly"
            and not data.get(CONF_HISTORY_BACKFILLED),
            subentry=subentry,
            account_number=data.get(CONF_ACCOUNT_NUMBER),
            legacy_ids=bool(data.get(CONF_LEGACY_IDS)),
        )
        entry_data["sensors"][subentry.subentry_id] = sensor

        # Never block platform setup on update_before_add: the first update
        # requires a full OAuth login (get_refresh_token() alone chains 4
        # sequential HTTP round-trips to the Azure B2C login pages, plus the
        # usage request itself), which routinely takes long enough to trip
        # HA's "setup is taking over 10 seconds" warning. Add the entity
        # immediately -- it kicks off its own first update from
        # async_added_to_hass() once hass/entity_id are actually set, rather
        # than from a background task started here that could theoretically
        # race the entity's own add-to-platform sequence.
        async_add_entities(
            [sensor],
            update_before_add=False,
            config_subentry_id=subentry.subentry_id,
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
        entry: ConfigEntry,
        *,
        needs_backfill: bool,
        subentry: ConfigSubentry | None = None,
        account_number: str | None = None,
        legacy_ids: bool = True,
    ) -> None:
        """
        Initialize Watercare Usage sensor.

        legacy_ids keeps the pre-subentry, account-less unique_id and
        statistic ids (see CONF_LEGACY_IDS); otherwise both include
        account_number, so several accounts can coexist.
        """
        self._subentry = subentry
        self._account_number = account_number
        self._legacy_ids = legacy_ids
        self._name = name if legacy_ids else f"{name} {account_number}"
        self._icon = "mdi:water"
        self._state = None
        self._unit_of_measurement = "L"
        self._unique_id = DOMAIN if legacy_ids else f"{DOMAIN}_{account_number}"
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
        self._entry = entry
        self._needs_backfill = needs_backfill
        self._unsub_scheduled_poll = None
        # Guards _get_data(): the retry ladder can call it many times a day
        # while credentials are actually broken, and async_start_reauth()
        # itself dedupes concurrent flows but not the log spam -- only warn
        # and start a flow once per outage, clearing on the next success.
        self._reauth_started = False
        # Serializes regular updates and deep backfills: a scheduled poll
        # landing mid-backfill would otherwise advance the append-only
        # statistics watermark past the backfill's older history, silently
        # discarding it (see async_backfill_halfhourly_history).
        self._update_lock = asyncio.Lock()
        # Whether the most recent update got no response at all -- drives
        # _should_poll_now's hourly recovery retries.
        self._last_update_failed = False
        # halfhourly polls once at the start hour (plus the stale-retry
        # ladder, see POLL_RETRY_SLOTS_UTC); other endpoints poll twice
        # daily (see SECONDARY_POLL_HOUR_UTC). (hour, minute) in UTC.
        self._daily_poll_slots = (
            ((POLL_START_HOUR_UTC, 0),)
            if endpoint == "halfhourly"
            else ((POLL_START_HOUR_UTC, 0), (SECONDARY_POLL_HOUR_UTC, 0))
        )
        # Latest complete NZ day confirmed in halfhourly readings -- drives
        # _is_data_fresh's retry decision. Left unset by endpoints other
        # than halfhourly, which don't publish on a characterized schedule
        # (see POLL_START_HOUR_UTC).
        self._latest_reading_date: date_type | None = None

    @property
    def should_poll(self) -> bool:
        """Disable default interval polling; fixed NZ times are scheduled ourselves."""
        return False

    async def async_added_to_hass(self) -> None:
        """
        Kick off the sensor's first data fetch.

        Runs from this lifecycle hook rather than from a task started
        alongside async_add_entities() in async_setup_entry so it can't
        possibly run before hass/entity_id are set on this entity -- HA
        only calls async_added_to_hass() once the entity is fully added
        to the platform. The fetch itself is held back until Home Assistant
        has finished starting (see _run_after_hass_started). Fixed-time
        polling isn't scheduled here: the standing tick subscription is
        registered once the initial backfill/update task finishes (see
        _run_backfill/_run_initial_update), so the first scheduled poll
        can't land mid-backfill; _update_lock additionally serializes any
        update against a backfill regardless of who started them.
        """
        await super().async_added_to_hass()
        if self._needs_backfill:
            self._entry.async_create_background_task(
                self.hass,
                self._run_after_hass_started(self._run_backfill),
                "watercare_history_backfill",
            )
        else:
            self._entry.async_create_background_task(
                self.hass,
                self._run_after_hass_started(self._run_initial_update),
                "watercare_initial_update",
            )

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the standing poll subscription."""
        await super().async_will_remove_from_hass()
        if self._unsub_scheduled_poll is not None:
            self._unsub_scheduled_poll()
            # Clearing the handle also tells an in-flight
            # _handle_scheduled_tick not to write state on this
            # now-defunct entity.
            self._unsub_scheduled_poll = None

    def _is_data_fresh(self, now_nz: datetime) -> bool:
        """
        Whether the most recent update captured a complete previous NZ day.

        Only halfhourly tracks this (see _bucket_hourly_readings) -- it's
        the only endpoint whose publish timing has been characterized (see
        POLL_START_HOUR_UTC); other endpoints poll on their fixed daily
        slots instead of retrying for a signal they don't have. For
        halfhourly, never having seen a complete day (fresh install, or
        the in-memory date lost to a restart before a successful update)
        counts as stale, so the retry ladder keeps trying rather than
        leaving the sensor empty until the next day's start hour.
        """
        if self._endpoint != "halfhourly":
            return True
        if self._latest_reading_date is None:
            return False
        return self._latest_reading_date >= now_nz.date() - timedelta(days=1)

    def _should_poll_now(self, now: datetime) -> bool:
        """
        Whether this quarter-hour tick (timezone-aware `now`) should hit the API.

        Three reasons to poll: the endpoint's fixed daily slot(s); the
        stale-retry ladder while the previous NZ day is incomplete; and
        hourly recovery retries (any hour of day) after an update that got
        no response at all, so a transient API/network failure doesn't
        leave the sensor stale until the next fixed slot -- the
        SCAN_INTERVAL polling this replaced recovered within 12h, and
        hourly is the same idea with a tighter bound.

        Slots are matched in UTC, the clock Watercare publishes on (see
        POLL_START_HOUR_UTC); freshness is judged by NZ calendar day.
        """
        now_utc = now.astimezone(UTC)
        slot = (now_utc.hour, now_utc.minute)
        if self._last_update_failed and now_utc.minute == 0:
            return True
        if (
            not self._is_data_fresh(now.astimezone(NZ_TIMEZONE))
            and slot in POLL_RETRY_SLOTS_UTC
        ):
            return True
        return slot in self._daily_poll_slots

    def _schedule_polling(self) -> None:
        """
        Register the standing quarter-hour poll tick (idempotent).

        One persistent async_track_time_change subscription instead of a
        re-armed chain of one-shot timers: there is no per-fire re-arm
        step whose failure could silently end all future polling, and no
        timer to leak or orphan if scheduling or teardown ever races an
        in-flight update. The tick fires at minutes 00/15/30/45 in HA's
        configured timezone -- every real-world UTC offset is a multiple
        of 15 minutes, so that grid coincides with the same UTC minutes no
        matter where HA runs; the callback decides whether this tick is
        one of the UTC poll slots (see _should_poll_now).
        """
        if self._unsub_scheduled_poll is not None:
            return
        self._unsub_scheduled_poll = async_track_time_change(
            self.hass,
            self._handle_scheduled_tick,
            minute=list(QUARTER_HOUR_MINUTES),
            second=0,
        )

    async def _handle_scheduled_tick(self, now: datetime) -> None:
        # The unsub-handle guards cover teardown races: the subscription
        # can fire one last time around entity removal, and an in-flight
        # update can outlive async_will_remove_from_hass -- never write
        # state on a defunct entity.
        if self._unsub_scheduled_poll is None:
            return
        if not self._should_poll_now(now):
            return
        # Nothing awaits this callback, so failures must be caught and
        # logged here rather than left to escape silently.
        try:
            await self.async_update()
        except Exception:
            _LOGGER.exception("Watercare scheduled update failed")
        if self._unsub_scheduled_poll is not None:
            self.async_write_ha_state()

    async def _run_after_hass_started(
        self, startup_job: Callable[[], Awaitable[None]]
    ) -> None:
        """
        Run `startup_job` once Home Assistant has finished starting.

        The recorder connects to its database early in startup but doesn't
        process its write queue until EVENT_HOMEASSISTANT_STARTED, so
        statistics pushed while HA is still starting just sit queued --
        and _async_await_recorder_flush's timeout would expire (and warn
        on every restart) whenever the rest of startup takes longer than
        that, with nothing actually wrong with the recorder. Waiting here
        instead means that timeout only ever measures a recorder that is
        actually draining its queue. Returns straight away if HA is
        already running (e.g. the config entry was reloaded).
        """
        started = asyncio.Event()

        @callback
        def _hass_started(_hass: HomeAssistant) -> None:
            started.set()

        unsub = async_at_started(self.hass, _hass_started)
        try:
            await started.wait()
        finally:
            # Unloading the config entry cancels this task; don't leave the
            # listener behind on a defunct entity.
            unsub()
        await startup_job()

    async def _run_backfill(self) -> None:
        # Nothing awaits this background task, so an unhandled exception
        # here would only surface as an asyncio "exception was never
        # retrieved" warning with no entity state update at all -- catch,
        # log, and still write whatever state was reached so the entity
        # doesn't just sit unavailable with no explanation.
        try:
            # The backfill must land its (older) history before the regular
            # update runs, since _async_push_hourly_statistic only appends
            # points after the latest already-stored one -- doing the
            # regular update first would set that watermark to "now" and
            # cause the backfill's data to be silently discarded as
            # already-superseded.
            await self.async_backfill_halfhourly_history()
            self.hass.config_entries.async_update_subentry(
                self._entry,
                self._subentry,
                data={**self._subentry.data, CONF_HISTORY_BACKFILLED: True},
            )
            await self.async_update()
        except Exception:
            _LOGGER.exception("Watercare history backfill failed")
        # Deliberately not a `finally`: unloading the config entry cancels
        # this task, and the CancelledError (a BaseException, so not caught
        # above) must skip both steps -- writing state on a removed entity
        # and registering a polling subscription nothing would ever cancel.
        self.async_write_ha_state()
        self._schedule_polling()

    async def _run_initial_update(self) -> None:
        # See _run_backfill: nothing awaits this task, so failures need to
        # be caught and logged here, and cancellation must skip the
        # post-steps below.
        try:
            await self.async_update()
        except Exception:
            _LOGGER.exception("Watercare initial update failed")
        self.async_write_ha_state()
        self._schedule_polling()

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

    @property
    def endpoint(self) -> str:
        """Return the data source (API endpoint) this sensor polls."""
        return self._endpoint

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
        return self._statistic_label(f"Watercare {endpoint_name} {type_name}")

    def _statistic_id(self, name: str) -> str:
        """Return the external statistic id for `name`, scoped to the account."""
        if self._legacy_ids:
            return f"{DOMAIN}:{name}"
        # Statistic ids only allow lowercase letters, digits and single
        # underscores; account numbers contain hyphens ("0012345-02").
        account_slug = re.sub(r"[^0-9a-z]+", "_", str(self._account_number).lower())
        return f"{DOMAIN}:{account_slug.strip('_')}_{name}"

    def _statistic_label(self, label: str) -> str:
        """Return a statistic's display name, tagged with the account if scoped."""
        if self._legacy_ids:
            return label
        return f"{label} ({self._account_number})"

    async def _get_data(
        self, endpoint: str, start_date: str | None, end_date: str | None
    ) -> str | None:
        """
        Fetch data, starting HA's reauth flow if Watercare rejects the credentials.

        A rejected password/refresh token isn't a transient failure the
        normal retry ladder can recover from -- surface it the standard HA
        way (a reauth prompt in Settings) instead of just logging forever.
        Only the first rejection during an outage warns/starts a flow (see
        _reauth_started); the retry ladder can otherwise call this many
        times a day and spam the log for the entire outage.
        """
        try:
            response = await self._api.get_data(
                endpoint=endpoint,
                start_date=start_date,
                end_date=end_date,
                account_number=self._account_number,
            )
        except WatercareAuthError as err:
            # Rejected credentials aren't transient -- don't leave the
            # hourly recovery-retry ladder (_should_poll_now) spinning on a
            # login that can't succeed until the user completes reauth.
            self._last_update_failed = False
            if not self._reauth_started:
                self._reauth_started = True
                _LOGGER.warning(
                    "Watercare credentials rejected; starting reauth flow: %s", err
                )
                self._entry.async_start_reauth(self.hass)
            return None
        if response is not None:
            # Only an actual successful response re-arms the guard -- an
            # unrelated transient failure (e.g. a non-200 from the usage
            # endpoint) also returns None without raising, and must not be
            # mistaken for recovery.
            self._reauth_started = False
            self._async_store_resolved_account_number()
        return response

    @callback
    def _async_store_resolved_account_number(self) -> None:
        """
        Record which account a migrated subentry has actually been polling.

        The subentry migrated from a pre-subentry entry starts without an
        account number and polls the login's first account; storing it makes
        that explicit, and (as the subentry's unique_id) stops the same
        account being added a second time as a separate subentry.
        """
        if self._account_number or self._subentry is None:
            return
        account_number = self._api.default_account_number
        if not account_number:
            return
        self._account_number = account_number
        try:
            self.hass.config_entries.async_update_subentry(
                self._entry,
                self._subentry,
                data={**self._subentry.data, CONF_ACCOUNT_NUMBER: account_number},
                title=account_number,
                unique_id=account_number,
            )
        except AbortFlow:
            _LOGGER.warning(
                "Watercare account %s is also set up as a separate account; "
                "remove one of them to avoid importing its usage twice",
                account_number,
            )

    async def async_update(self) -> None:
        """Update the sensor data."""
        # The lock serializes this against a concurrently running deep
        # backfill (see _update_lock); a poll landing mid-backfill would
        # otherwise advance the statistics watermark past the backfill's
        # older history.
        async with self._update_lock:
            _LOGGER.debug("Beginning sensor update using endpoint: %s", self._endpoint)

            start_date = None
            end_date = None
            if self._endpoint == "halfhourly":
                # Unlike the other endpoints, halfhourly has no server-side
                # default range and 400s (INVALID_REQUEST_BODY) without one.
                today = datetime.now(NZ_TIMEZONE).date()
                start_date = (
                    today - timedelta(days=HALFHOURLY_POLL_WINDOW_DAYS)
                ).strftime("%Y-%m-%d")
                end_date = today.strftime("%Y-%m-%d")
            elif self._endpoint in ("dailywithstats", "monthly"):
                # These also have no server-side default range and 400 without
                # one, but (unlike halfhourly) aren't capped by request span and
                # their processing recomputes cumulative sums from the full
                # response each poll, so request the full history span.
                today = datetime.now(NZ_TIMEZONE).date()
                start_date = (
                    today - timedelta(days=FULL_HISTORY_LOOKBACK_DAYS)
                ).strftime("%Y-%m-%d")
                end_date = today.strftime("%Y-%m-%d")

            # Assume failure until a response actually arrives, so an
            # exception anywhere in the fetch also leaves the flag set and
            # _should_poll_now schedules hourly recovery retries.
            self._last_update_failed = True
            response = await self._get_data(
                endpoint=self._endpoint, start_date=start_date, end_date=end_date
            )
            if response is not None:
                self._last_update_failed = False
            elif self._reauth_started:
                # Reauth already surfaces this to the user (one deduped
                # warning + a Settings prompt) -- skip the generic "no
                # response" error every poll while it's pending, so the
                # hourly recovery ladder doesn't spam the log for the
                # entire outage.
                return

            # Route to appropriate processing method based on endpoint
            if self._endpoint == "dailywithstats":
                await self.process_daily_data(response)
            elif self._endpoint == "halfhourly":
                # halfhourly returns a bare list of {timestamp, litres}
                # readings, not billing periods or a dailywithstats-style
                # wrapper.
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
            statistic_id=self._statistic_id("water_consumption"),
            name=self._statistic_label("Watercare Water Consumption"),
            unit=self._unit_of_measurement,
            unit_class=VolumeConverter.UNIT_CLASS,
            empty_warning="No valid consumption statistics generated",
        )
        push_statistic_series(
            self.hass,
            cost_points,
            statistic_id=self._statistic_id("water_cost"),
            name=self._statistic_label("Watercare Total Cost"),
            unit="NZD",
            unit_class=None,
            empty_warning="No valid cost statistics generated",
        )
        push_statistic_series(
            self.hass,
            consumption_cost_points,
            statistic_id=self._statistic_id("consumption_cost"),
            name=self._statistic_label("Watercare Consumption Cost"),
            unit="NZD",
            unit_class=None,
            empty_warning=None,
        )
        push_statistic_series(
            self.hass,
            wastewater_cost_points,
            statistic_id=self._statistic_id("wastewater_cost"),
            name=self._statistic_label("Watercare Wastewater Cost"),
            unit="NZD",
            unit_class=None,
            empty_warning=None,
        )
        await self._async_await_recorder_flush()

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
            statistic_id=self._statistic_id("monthly_consumption"),
            name=self._get_statistic_name("consumption"),
            unit=self._unit_of_measurement,
            unit_class=VolumeConverter.UNIT_CLASS,
            empty_warning="No valid monthly consumption statistics generated",
        )
        push_statistic_series(
            self.hass,
            cost_points,
            statistic_id=self._statistic_id("monthly_cost"),
            name=self._statistic_label("Watercare Monthly Cost"),
            unit="NZD",
            unit_class=None,
            empty_warning="No valid monthly cost statistics generated",
        )
        push_statistic_series(
            self.hass,
            consumption_cost_points,
            statistic_id=self._statistic_id("monthly_consumption_cost"),
            name=self._statistic_label("Watercare Monthly Consumption Cost"),
            unit="NZD",
            unit_class=None,
            empty_warning=None,
        )
        push_statistic_series(
            self.hass,
            wastewater_cost_points,
            statistic_id=self._statistic_id("monthly_wastewater_cost"),
            name=self._statistic_label("Watercare Monthly Wastewater Cost"),
            unit="NZD",
            unit_class=None,
            empty_warning=None,
        )
        await self._async_await_recorder_flush()

    @staticmethod
    def _parse_reading_time(timestamp_str: str | None) -> datetime | None:
        """Parse a halfhourly reading's UTC API timestamp into NZ local time."""
        if not timestamp_str:
            return None
        try:
            reading_time = datetime.strptime(
                timestamp_str, "%Y-%m-%dT%H:%M:%S.%fZ"
            ).replace(tzinfo=UTC)
        except ValueError:
            _LOGGER.warning("Failed to parse timestamp %s", timestamp_str)
            return None
        return reading_time.astimezone(NZ_TIMEZONE)

    def _bucket_hourly_readings(
        self, readings: list[dict[str, Any]]
    ) -> tuple[dict[datetime, float], date_type | None]:
        """
        Bucket a list of {timestamp, litres} readings into hourly sums.

        Also returns the latest NZ date whose halfhourly data is confirmed
        fully published, computed in the same pass over `readings` rather
        than re-parsing timestamps separately (this can run on years of
        backfilled readings, not just a single poll's response). A day
        only counts as complete once every one of its half-hour slots has
        been published -- Watercare returns zero-litre slots rather than
        omitting them (the recorder shows hundreds of zero-use hours), so
        a missing slot means the publish isn't finished. Seen live
        mid-publish: a 23:00-only reading for the day (23:30 landed 15 min
        later), and, once polls moved to UTC and started landing inside
        the publish window, a response with only a handful of the day's
        slots that still included 23:30. Either would otherwise be
        mistaken for a complete day, and _is_data_fresh would stop
        retrying with most of the day unwritten.
        """
        hourly_consumption: dict[datetime, float] = {}
        # Slots seen per NZ date, keyed by UTC instant: on the DST-end day
        # the two 02:30 NZDT/NZST readings compare equal as NZ-local
        # datetimes (fold is ignored within one zone), and would collapse
        # into one slot.
        slots_by_date: dict[date_type, set[float]] = {}
        for reading in readings:
            reading_time = self._parse_reading_time(reading.get("timestamp"))
            if reading_time is None:
                continue
            hour_start = reading_time.replace(minute=0, second=0, microsecond=0)
            litres = reading.get("litres", 0)
            hourly_consumption[hour_start] = (
                hourly_consumption.get(hour_start, 0) + litres
            )
            slots_by_date.setdefault(reading_time.date(), set()).add(
                reading_time.timestamp()
            )
        complete_dates = (
            day
            for day, slots in slots_by_date.items()
            if len(slots) >= self._half_hour_slots_in_nz_day(day)
        )
        return hourly_consumption, max(complete_dates, default=None)

    @staticmethod
    def _half_hour_slots_in_nz_day(day: date_type) -> int:
        """Half-hour slots in an NZ calendar day: 48, or 46/50 across DST."""
        start = datetime(day.year, day.month, day.day, tzinfo=NZ_TIMEZONE)
        next_day = day + timedelta(days=1)
        end = datetime(next_day.year, next_day.month, next_day.day, tzinfo=NZ_TIMEZONE)
        return int((end.timestamp() - start.timestamp()) // 1800)

    def _advance_latest_reading_date(
        self, latest_complete_date: date_type | None
    ) -> None:
        """
        Advance the freshness watermark, never regressing it.

        A response with no confirmed-complete day, or one older than
        what's already known (e.g. a truncated/narrower window than
        expected), must not regress an already-known complete date and
        re-trigger retries for a day that was already captured.
        """
        if latest_complete_date is not None and (
            self._latest_reading_date is None
            or latest_complete_date > self._latest_reading_date
        ):
            self._latest_reading_date = latest_complete_date

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

    async def _async_statistics_window(
        self, statistic_id: str, since: datetime
    ) -> tuple[float, dict[datetime, float]]:
        """
        Return the (anchor sum, already-stored hours) inputs a healing window needs.

        The anchor sum is the cumulative sum just before `since`; the
        already-stored hours are {hour_start: sum} for hours at/after
        `since` that _async_heal_hourly_points needs to rebuild the window.

        `_HEAL_LOOKBACK_STATS` rows is comfortably more than the halfhourly
        endpoint's 7-day (168-hour) fetch window even if most of those hours
        are sparse/missing (the exact scenario this is healing), so the
        anchor before `since` is always found among the rows this one query
        returns. If it isn't -- `since` is further back than the lookback
        reaches -- there's no stored data yet to anchor to, so anchor_sum
        defaults to 0.0, same as a brand-new statistic.
        """
        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            _HEAL_LOOKBACK_STATS,
            statistic_id,
            True,  # noqa: FBT003 -- positional per get_last_statistics' signature
            {"sum"},
        )
        since_ts = since.timestamp()
        anchor_sum = 0.0
        existing_sums: dict[datetime, float] = {}
        # Rows come back newest-first (see get_last_statistics), so the
        # first one older than `since` is the closest anchor.
        for row in last_stat.get(statistic_id, []):
            start_ts = row["start"]
            if start_ts >= since_ts:
                existing_sums[datetime.fromtimestamp(start_ts, tz=NZ_TIMEZONE)] = float(
                    row.get("sum") or 0.0
                )
            else:
                anchor_sum = float(row.get("sum") or 0.0)
                break
        return anchor_sum, existing_sums

    async def _async_heal_hourly_points(
        self,
        hourly_consumption: dict[datetime, float],
        *,
        statistic_id: str,
        heal_since: datetime,
        cost_key: str | None,
        fresh_hours: set[datetime] | None = None,
    ) -> list[StatisticData]:
        """
        Rebuild every hour from `heal_since` onward.

        Mixes this poll's fresh readings with whatever's already stored for
        hours this poll didn't return. Fixes a gap class the plain
        append-only push (see
        _async_push_hourly_statistic) can't: Watercare's API has been
        observed to return a sparse set of half-hourly readings for an
        earlier day in the window than the one _bucket_hourly_readings
        confirms complete (only the latest complete date is checked slot by
        slot), so the resulting push is missing whichever hours weren't in
        that response -- and since the watermark only advances, a later
        poll with a fuller response for the same day could never fill them
        in. Recomputing the whole window from an
        anchor before it and upserting (async_add_external_statistics
        updates an existing point for a given start rather than duplicating
        it) means a hole from one poll's sparse response gets healed the
        next time the API returns more for that hour, with no risk of
        clobbering an hour this poll didn't see: those keep their already-
        stored sum via `existing_sums`, they're just not re-sent as-is --
        see the delta carry-forward below for why they can still change.

        Any `hourly_consumption` hour before `heal_since` is ignored -- it's
        outside the window `anchor_sum` accounts for, so folding it into the
        running sum would double-count it against whatever's already stored
        there.

        `fresh_hours` marks which hours actually changed this poll and
        should be recomputed from `hourly_consumption`; every other hour
        carries its existing stored sum forward instead (see `delta`
        below). Defaults to every hour with a reading this poll -- the
        right behavior for the plain consumption series, where "has a
        reading" and "may have changed" are the same thing. Cost series
        pass the *consumption* series' own touched-hour set instead: cost
        is a pure function of litres and the currently configured rate, so
        recomputing it for the whole window unconditionally would silently
        rewrite already-published cost history the moment the user changes
        a rate, even for hours whose litres never changed. Restricting to
        the hours consumption actually touched means only a genuine litres
        change (fresh data, or a delta cascading from an earlier fix, see
        below) can move cost history -- a rate change alone can't.

        A hole (no fresh, unrestricted reading this hour) carries forward
        via `delta`: the gap between the running sum this function has
        just computed and the sum that was already stored for the last
        hour it touched. Without this, healing an earlier hour upward (or
        downward) while a later hour in the same window goes untouched
        would leave the later hour's stored sum unchanged -- producing a
        non-monotonic sum sequence (a decreasing "cumulative" total) the
        moment the two are compared, e.g. in the Energy dashboard. Shifting
        every untouched hour by the same delta keeps the whole window
        internally consistent, and reduces to a no-op (delta stays 0, no
        extra writes) whenever nothing needed correcting.

        A recomputed hour whose sum comes out unchanged from what's already
        stored (within floating-point tolerance -- cost series in
        particular can re-derive a value a rounding ULP off from the
        original even when nothing actually changed) is left out of the
        returned points -- healing re-touches the whole window every poll,
        so without this most hours would upsert a functionally-unchanged
        value every single time.
        """
        if fresh_hours is None:
            fresh_hours = set(hourly_consumption)

        anchor_sum, existing_sums = await self._async_statistics_window(
            statistic_id, heal_since
        )
        healed_hours = {
            hour for hour in hourly_consumption if hour >= heal_since
        } | set(existing_sums)
        running_sum = anchor_sum
        delta = 0.0
        points = []
        for hour_start in sorted(healed_hours):
            old_sum = existing_sums.get(hour_start)
            # Always recompute an hour with no existing stored sum at all
            # (nothing to carry forward from), even if it's outside
            # fresh_hours -- e.g. a cost series that was previously gated
            # off by a zero rate and has no history yet to preserve.
            if hour_start in hourly_consumption and (
                hour_start in fresh_hours or old_sum is None
            ):
                litres = hourly_consumption[hour_start]
                if cost_key is None:
                    running_sum += litres
                else:
                    running_sum += self._calculate_cost(litres, 1 / 24)[cost_key]
                if old_sum is not None:
                    delta = running_sum - old_sum
                if old_sum is None or not math.isclose(
                    old_sum, running_sum, rel_tol=1e-9, abs_tol=1e-9
                ):
                    points.append(StatisticData(start=hour_start, sum=running_sum))
            else:
                # Not touched this poll -- carry its already-stored sum
                # forward, shifted by whatever correction delta the most
                # recently touched hour introduced (see delta above).
                running_sum = existing_sums[hour_start] + delta
                if not math.isclose(delta, 0.0, abs_tol=1e-9):
                    points.append(StatisticData(start=hour_start, sum=running_sum))
        return points

    async def _async_await_recorder_flush(self) -> None:
        """
        Wait for the recorder to actually persist whatever was just queued.

        push_statistic_series (async_add_external_statistics) only queues
        the write onto the recorder's background thread and returns
        immediately -- it does not wait for the write to reach the
        database. An HA restart landing in that window would silently
        lose the queued points with no exception raised; for the
        append-only halfhourly series in particular, no later poll would
        ever re-fetch and re-write them, since the watermark only ever
        advances (observed live: restarts during a failed HACS update
        fragmented several days of halfhourly statistics this way).
        Callers should call this once after queuing everything for one
        update cycle, not per series -- the recorder processes its queue
        FIFO, so one wait after the last queue op covers all of them.

        Bounded by a timeout so a stalled/dead recorder delays this one
        update cycle rather than blocking _update_lock -- and therefore
        every future poll -- forever.
        """
        try:
            await asyncio.wait_for(
                get_instance(self.hass).async_block_till_done(),
                timeout=_RECORDER_FLUSH_TIMEOUT,
            )
        except TimeoutError:
            _LOGGER.warning(
                "Recorder did not confirm pending Watercare statistics writes "
                "within %ss; continuing without confirmation",
                _RECORDER_FLUSH_TIMEOUT,
            )

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
        heal_since: datetime | None = None,
        fresh_hours: set[datetime] | None = None,
        touched_hours: set[datetime] | None = None,
    ) -> int:
        """
        Push one hourly external-statistics series.

        Continues from whatever cumulative sum is already stored for
        `statistic_id` rather than resetting to zero.

        With `heal_since` unset (the deep one-off backfill's usage -- see
        async_backfill_halfhourly_history), only appends hours strictly
        after the last stored one, append-only, so a run over years of
        history doesn't requery/resend all of it every time.

        With `heal_since` set (the regular poll's usage -- see
        process_halfhourly_data), rebuilds every hour from `heal_since`
        onward instead (see _async_heal_hourly_points): the regular poll's
        fetch window is only ~7 days, cheap to fully requery, and doing so
        heals any gap a previous poll's sparse API response left behind.
        `fresh_hours` is forwarded to it unchanged (see its docstring for
        why a cost series passes something narrower than "every hour with
        a reading").

        If `touched_hours` is given, it's updated with the start of every
        point actually written this call -- so a caller pushing several
        series in sequence (see _async_push_halfhourly_statistics) can feed
        one series' result in as the next series' `fresh_hours`.

        Returns the number of points actually written -- callers that
        expect to backfill older history should check this, since it will
        be 0 if `hourly_consumption` is entirely at or before the existing
        watermark (heal_since unset) or contributes nothing new (heal_since
        set).
        """
        if rate_gate is not None and rate_gate <= 0:
            return 0

        if heal_since is not None:
            new_points = await self._async_heal_hourly_points(
                hourly_consumption,
                statistic_id=statistic_id,
                heal_since=heal_since,
                cost_key=cost_key,
                fresh_hours=fresh_hours,
            )
        else:
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

        if touched_hours is not None:
            touched_hours.update(point["start"] for point in new_points)

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
        self,
        hourly_consumption: dict[datetime, float],
        *,
        heal_since: datetime | None = None,
    ) -> int:
        """
        Push all four hourly statistic series for the halfhourly endpoint.

        See _async_push_hourly_statistic for what `heal_since` changes.
        Consumption is pushed first (only it can decide which hours
        genuinely changed -- see _async_heal_hourly_points), then the three
        cost series run concurrently, each restricted to that same
        touched-hour set so a rate change alone can't rewrite already-
        published cost history.

        Returns the number of new consumption points written (the other
        three series share the same watermark and will match, unless
        gated off entirely by a zero rate).
        """
        touched_hours: set[datetime] | None = set() if heal_since is not None else None
        new_count = await self._async_push_hourly_statistic(
            hourly_consumption,
            statistic_id=self._statistic_id("halfhourly_consumption"),
            name=self._get_statistic_name("consumption"),
            unit=self._unit_of_measurement,
            cost_key=None,
            rate_gate=None,
            unit_class=VolumeConverter.UNIT_CLASS,
            heal_since=heal_since,
            touched_hours=touched_hours,
        )
        await asyncio.gather(
            self._async_push_hourly_statistic(
                hourly_consumption,
                statistic_id=self._statistic_id("halfhourly_cost"),
                name=self._statistic_label("Watercare Half-hourly Cost"),
                unit="NZD",
                cost_key="total",
                rate_gate=None,
                heal_since=heal_since,
                fresh_hours=touched_hours,
            ),
            self._async_push_hourly_statistic(
                hourly_consumption,
                statistic_id=self._statistic_id("halfhourly_consumption_cost"),
                name=self._statistic_label("Watercare Half-hourly Consumption Cost"),
                unit="NZD",
                cost_key="consumption",
                rate_gate=self._consumption_rate,
                heal_since=heal_since,
                fresh_hours=touched_hours,
            ),
            self._async_push_hourly_statistic(
                hourly_consumption,
                statistic_id=self._statistic_id("halfhourly_wastewater_cost"),
                name=self._statistic_label("Watercare Half-hourly Wastewater Cost"),
                unit="NZD",
                cost_key="wastewater",
                rate_gate=self._wastewater_rate,
                heal_since=heal_since,
                fresh_hours=touched_hours,
            ),
        )
        await self._async_await_recorder_flush()
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

        hourly_consumption, latest_complete_date = self._bucket_hourly_readings(
            readings
        )
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

        # Only push statistics for confirmed-complete days. A poll that
        # catches Watercare mid-publish (observed live: the 06:14 poll saw
        # yesterday up to 23:00 with no 23:30 yet; the 07:15 NZDT poll saw
        # only a handful of yesterday's slots) would otherwise write partial
        # buckets and advance the append-only watermark past them,
        # permanently excluding the late-arriving litres from the
        # cumulative sums. Deferred days are re-fetched by the retry ladder
        # (and the 7-day request window) once complete.
        if latest_complete_date is None:
            _LOGGER.warning(
                "Halfhourly response contained no confirmed-complete day; "
                "deferring statistics push until one appears"
            )
            return
        complete_day_buckets = {
            hour: litres
            for hour, litres in hourly_consumption.items()
            if hour.date() <= latest_complete_date
        }
        # Heal the whole fetched window, not just append past the
        # watermark: Watercare's API has been observed to return a sparse
        # set of readings, and only the latest complete day is checked slot
        # by slot, so a later, fuller response for an earlier day needs to
        # be able to fill in what an earlier poll's push missed (see
        # _async_heal_hourly_points).
        await self._async_push_halfhourly_statistics(
            complete_day_buckets, heal_since=min(complete_day_buckets)
        )

        # Advance the freshness watermark only now that the push has
        # succeeded -- a failed push must leave the day stale so the retry
        # ladder re-attempts it instead of stopping for the day with
        # yesterday's statistics unwritten.
        self._advance_latest_reading_date(latest_complete_date)

    async def async_backfill_halfhourly_history(self) -> None:
        """
        One-time deep import of halfhourly history beyond the normal rolling window.

        The API silently caps each request at ~161 days regardless of the
        requested range (it always starts exactly at the requested `from`
        date), so this pages backward in fixed-size chunks.
        """
        # The lock serializes this against regular updates: the paging
        # below takes many seconds, and a scheduled poll (or the
        # watercare.backfill_history service racing one) landing mid-way
        # would push current-window buckets and advance the append-only
        # watermark, silently discarding this older history.
        async with self._update_lock:
            await self._async_backfill_halfhourly_history_locked()

    async def _async_backfill_halfhourly_history_locked(self) -> None:
        _LOGGER.info("Starting Watercare halfhourly history backfill")
        today = datetime.now(NZ_TIMEZONE).date()
        chunk_end = today
        all_readings: dict[str, float] = {}

        while (today - chunk_end).days < HALFHOURLY_BACKFILL_MAX_DAYS:
            chunk_start = chunk_end - timedelta(days=HALFHOURLY_BACKFILL_CHUNK_DAYS)
            response = await self._get_data(
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

        hourly_consumption, latest_complete_date = self._bucket_hourly_readings(
            [
                {"timestamp": timestamp, "litres": litres}
                for timestamp, litres in all_readings.items()
            ]
        )
        # Same partial-day guard as process_halfhourly_data: a backfill run
        # mid-publish must not write an incomplete trailing hour that the
        # append-only watermark would then lock in. No complete day at all
        # (e.g. a brand-new meter whose only history is today's first few
        # hours) defers the push entirely -- regular polling will write it
        # once a complete day exists.
        if latest_complete_date is None:
            _LOGGER.warning(
                "Backfill collected no confirmed-complete day; deferring "
                "statistics push until one appears"
            )
            return
        hourly_consumption = {
            hour: litres
            for hour, litres in hourly_consumption.items()
            if hour.date() <= latest_complete_date
        }

        # Pushing is append-only (see _async_push_hourly_statistic): it only
        # writes hours after the statistic's existing watermark. If regular
        # polling has already run and advanced that watermark, none of this
        # deep-history data -- which is by definition older -- can actually
        # be written, even though the API calls above succeeded.
        consumption_statistic_id = self._statistic_id("halfhourly_consumption")
        _, last_start = await self._async_last_statistic_sum(consumption_statistic_id)
        oldest_collected = min(hourly_consumption)
        if last_start is not None and oldest_collected.timestamp() <= last_start:
            _LOGGER.warning(
                "Backfill collected %s hourly buckets back to %s, but existing "
                "halfhourly statistics already start at or before that point "
                "(from regular polling) -- history is only ever appended after "
                "the latest stored hour, so this older data won't be written. "
                "To re-import deeper history, clear this account's halfhourly "
                "statistics (%s and its cost series) first (Developer tools > "
                "Statistics) and re-run this service",
                len(hourly_consumption),
                oldest_collected,
                consumption_statistic_id,
            )

        new_count = await self._async_push_halfhourly_statistics(hourly_consumption)
        # Seed the freshness watermark from what the backfill actually
        # observed (after the push, mirroring process_halfhourly_data), so
        # a failed post-backfill regular update doesn't leave the sensor
        # thinking it has never seen a complete day.
        self._advance_latest_reading_date(latest_complete_date)
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
            statistic_id=self._statistic_id("daily_consumption"),
            name=self._get_statistic_name("consumption"),
            unit=self._unit_of_measurement,
            unit_class=VolumeConverter.UNIT_CLASS,
            empty_warning="No daily statistics found, skipping update",
        )
        push_statistic_series(
            self.hass,
            cost_points,
            statistic_id=self._statistic_id("daily_cost"),
            name=self._statistic_label("Watercare Daily Cost"),
            unit="NZD",
            unit_class=None,
            empty_warning=None,
        )
        push_statistic_series(
            self.hass,
            consumption_cost_points,
            statistic_id=self._statistic_id("daily_consumption_cost"),
            name=self._statistic_label("Watercare Daily Consumption Cost"),
            unit="NZD",
            unit_class=None,
            empty_warning=None,
        )
        push_statistic_series(
            self.hass,
            wastewater_cost_points,
            statistic_id=self._statistic_id("daily_wastewater_cost"),
            name=self._statistic_label("Watercare Daily Wastewater Cost"),
            unit="NZD",
            unit_class=None,
            empty_warning=None,
        )
        await self._async_await_recorder_flush()
