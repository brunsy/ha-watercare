"""
Shared fixtures for the watercare test suite.

Avoids pulling in a full pytest-homeassistant-custom-component `hass`/recorder
fixture: sensor.py and statistics_helpers.py only touch `hass` by passing it
through to a handful of recorder functions imported by name
(`async_add_external_statistics`, `get_last_statistics`, `get_instance`), so
those are monkeypatched directly with in-memory fakes instead.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from custom_components.watercare import sensor as watercare_sensor
from custom_components.watercare import statistics_helpers
from custom_components.watercare.const import NZ_TIMEZONE

if TYPE_CHECKING:
    from collections.abc import Callable

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    """Return the raw text of a fixture file under tests/fixtures/."""
    return (FIXTURES_DIR / name).read_text()


def nz_timestamp(days_ago: int, hour: int) -> str:
    """Build a UTC API timestamp landing `days_ago` days back at `hour` in NZ time."""
    nz_now = datetime.now(NZ_TIMEZONE)
    target_date = (nz_now - timedelta(days=days_ago)).date()
    nz_dt = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        hour,
        0,
        0,
        tzinfo=NZ_TIMEZONE,
    )
    return nz_dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class _FakeRecorderInstance:
    async def async_add_executor_job(self, func: Callable, *args: Any) -> Any:
        return func(*args)


@pytest.fixture(autouse=True)
def patch_recorder(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """
    Replace recorder/statistics calls with in-memory fakes.

    Exposes `.written` (statistic_id -> list of StatisticData actually
    pushed via async_add_external_statistics) and `.last_statistics`
    (statistic_id -> (sum, start_ts), settable per-test to simulate an
    existing watermark before calling the code under test).
    """
    written: dict[str, list] = {}
    last_statistics: dict[str, tuple[float, float]] = {}

    def fake_get_last_statistics(
        _hass: Any,
        _number: int,
        statistic_id: str,
        _convert_units: bool,  # noqa: FBT001
        _types: set[str],
    ) -> dict[str, list[dict[str, Any]]]:
        if statistic_id not in last_statistics:
            return {}
        total, start_ts = last_statistics[statistic_id]
        return {statistic_id: [{"sum": total, "start": start_ts}]}

    def fake_async_add_external_statistics(
        _hass: Any,
        metadata: Any,
        stats: list,
    ) -> None:
        # StatisticMetaData/StatisticData are TypedDicts (plain dicts at
        # runtime), not objects -- index them, don't attribute-access them.
        written.setdefault(metadata["statistic_id"], []).extend(stats)

    monkeypatch.setattr(
        watercare_sensor, "get_last_statistics", fake_get_last_statistics
    )
    monkeypatch.setattr(
        statistics_helpers,
        "async_add_external_statistics",
        fake_async_add_external_statistics,
    )
    monkeypatch.setattr(
        watercare_sensor, "get_instance", lambda _hass: _FakeRecorderInstance()
    )

    return SimpleNamespace(written=written, last_statistics=last_statistics)


@pytest.fixture
def make_sensor() -> Callable[..., watercare_sensor.WatercareUsageSensor]:
    """
    Return a factory that builds a WatercareUsageSensor.

    Constructs the sensor directly, bypassing entity-platform setup.
    """

    def _make(
        endpoint: str = "halfhourly",
        *,
        consumption_rate: float = 2.296,
        wastewater_rate: float = 3.994,
        wastewater_ratio: float = 0.785,
        annual_line_charge: float = 310,
    ) -> watercare_sensor.WatercareUsageSensor:
        sensor = watercare_sensor.WatercareUsageSensor(
            "Watercare",
            MagicMock(),
            consumption_rate,
            wastewater_rate,
            wastewater_ratio,
            annual_line_charge,
            endpoint,
        )
        sensor.hass = MagicMock()
        return sensor

    return _make


@pytest.fixture
def load_json_fixture() -> Callable[[str], Any]:
    """Return a callable that parses a fixture file's JSON content."""

    def _load(name: str) -> Any:
        return json.loads(load_fixture(name))

    return _load
