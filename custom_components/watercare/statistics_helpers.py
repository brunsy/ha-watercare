"""Shared helpers for building and pushing running-sum external statistics."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics

from .const import DOMAIN

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def accumulate_cost_series(
    entries: list[tuple[datetime, float, float]],
    *,
    calculate_cost: Callable[[float, float], dict[str, float]],
    consumption_rate: float,
    wastewater_rate: float,
    last_reset: datetime | None = None,
) -> tuple[
    list[StatisticData],
    list[StatisticData],
    list[StatisticData],
    list[StatisticData],
]:
    """
    Build running-sum consumption/cost/consumption_cost/wastewater_cost series.

    `entries` is (start, litres, number_of_days_for_cost) tuples, oldest
    first. `last_reset`, if given, is stamped onto every point of the
    consumption series only (matching the fixed billing-period-start reset
    used by dailywithstats' cumulative statistic).
    """
    consumption_points = []
    cost_points = []
    consumption_cost_points = []
    wastewater_cost_points = []
    running_sum = 0
    cost_running_sum = 0
    consumption_cost_running_sum = 0
    wastewater_cost_running_sum = 0

    for start, litres, number_of_days in entries:
        running_sum += litres
        cost_breakdown = calculate_cost(litres, number_of_days)
        cost_running_sum += cost_breakdown["total"]
        consumption_cost_running_sum += cost_breakdown["consumption"]
        wastewater_cost_running_sum += cost_breakdown["wastewater"]

        if last_reset is not None:
            consumption_points.append(
                StatisticData(start=start, sum=running_sum, last_reset=last_reset)
            )
        else:
            consumption_points.append(StatisticData(start=start, sum=running_sum))
        cost_points.append(StatisticData(start=start, sum=cost_running_sum))
        if consumption_rate > 0:
            consumption_cost_points.append(
                StatisticData(start=start, sum=consumption_cost_running_sum)
            )
        if wastewater_rate > 0:
            wastewater_cost_points.append(
                StatisticData(start=start, sum=wastewater_cost_running_sum)
            )

    return (
        consumption_points,
        cost_points,
        consumption_cost_points,
        wastewater_cost_points,
    )


def push_statistic_series(  # noqa: PLR0913 -- one series definition per call keeps push callers declarative; bundling into a dataclass would ripple with no behavior change
    hass: HomeAssistant,
    points: list[StatisticData],
    *,
    statistic_id: str,
    name: str,
    unit: str,
    unit_class: str | None,
    empty_warning: str | None,
) -> None:
    """Push one running-sum external-statistics series, or warn if empty."""
    if not points:
        if empty_warning:
            _LOGGER.warning(empty_warning)
        return

    metadata = StatisticMetaData(
        has_sum=True,
        name=name,
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_of_measurement=unit,
        mean_type=StatisticMeanType.NONE,
        unit_class=unit_class,
    )
    _LOGGER.debug("Adding %s statistics for %s", len(points), statistic_id)
    async_add_external_statistics(hass, metadata, points)
