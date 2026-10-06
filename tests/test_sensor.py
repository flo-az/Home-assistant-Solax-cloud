"""Tests for the Solax Cloud sensors while the integration is running."""

from __future__ import annotations

from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
import requests_mock as rm

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .common import (
    API_URL,
    FAILED_REQUESTS,
    async_poll,
    async_setup,
    ok_response,
    state,
)


@pytest.mark.parametrize("response", FAILED_REQUESTS)
async def test_regression_failed_poll_marks_sensors_unavailable(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    solax_api: rm.Mocker,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    response: dict[str, Any],
) -> None:
    """Regression: a refused poll froze every sensor on its last value.

    solaxcloud returns None for success=false (token revoked, request refused).
    The coordinator stored that as fresh data, every sensor raised
    AttributeError on None.get() and kept showing the previous reading as if
    it were current.
    """
    await async_setup(hass, config_entry)
    assert state(hass, "acpower") == "2224.0"

    solax_api.get(API_URL, **response)
    await async_poll(hass, freezer)

    assert state(hass, "acpower") == STATE_UNAVAILABLE
    assert state(hass, "total_solar_power") == STATE_UNAVAILABLE
    assert "AttributeError" not in caplog.text


async def test_sensors_recover_after_failed_poll(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    solax_api: rm.Mocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The next good poll brings sensors back with the new readings."""
    await async_setup(hass, config_entry)
    solax_api.get(API_URL, json={"success": False, "result": None, "code": 2001})
    await async_poll(hass, freezer)
    assert state(hass, "acpower") == STATE_UNAVAILABLE

    solax_api.get(API_URL, json=ok_response(acpower=1000.0))
    await async_poll(hass, freezer)

    assert state(hass, "acpower") == "1000.0"


async def test_missing_reading_is_unknown(
    hass: HomeAssistant, config_entry: MockConfigEntry, requests_mock: rm.Mocker
) -> None:
    """A null field (e.g. no battery) leaves its sensor unknown, not zero."""
    requests_mock.get(API_URL, json=ok_response(soc=None, batPower=None))

    await async_setup(hass, config_entry)

    assert state(hass, "soc") == STATE_UNKNOWN
    assert state(hass, "batPower") == STATE_UNKNOWN


@pytest.mark.parametrize(
    ("time_zone", "upload_time", "utc_date_time", "expected"),
    [
        pytest.param(
            "Europe/Berlin",
            "2026-10-06 14:33:04",
            "2026-10-06T06:33:04Z",
            "2026-10-06T12:33:04+00:00",
            id="live-sample-cest",
        ),
        # The sample from the comment that introduced the +7 h correction,
        # assuming that plant also ran on UTC+01:00.
        pytest.param(
            "Europe/Berlin",
            "2025-12-28 17:43:55",
            "2025-12-28T09:43:55Z",
            "2025-12-28T16:43:55+00:00",
            id="original-report-cet",
        ),
    ],
)
async def test_regression_utc_date_time_is_upload_instant(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    requests_mock: rm.Mocker,
    time_zone: str,
    upload_time: str,
    utc_date_time: str,
    expected: str,
) -> None:
    """Regression: utcDateTime was shifted by a fixed +7 h.

    Solax derives utcDateTime by reading the plant's local wall time as
    UTC+8, so it lags the real instant by 8 h minus the plant's UTC offset:
    7 h in CET winter, 6 h in CEST summer. The fixed +7 h put the summer
    timestamp one hour in the future.
    """
    await hass.config.async_set_time_zone(time_zone)
    requests_mock.get(
        API_URL, json=ok_response(uploadTime=upload_time, utcDateTime=utc_date_time)
    )

    await async_setup(hass, config_entry)

    assert state(hass, "utcDateTime") == expected


@pytest.mark.parametrize(
    ("key", "code", "expected"),
    [
        # SolaXCloud User API V1.2, 8.1 "Device Status Mapping" and batStatus.
        ("inverterStatus", "100", "waiting"),
        ("inverterStatus", "102", "normal"),
        ("inverterStatus", "107", "off_grid"),
        ("inverterStatus", "109", "sleep"),
        ("inverterStatus", "148", "normal_ss"),
        ("inverterStatus", "160", "openadr"),
        ("inverterStatus", "999", STATE_UNKNOWN),
        ("inverterStatus", None, STATE_UNKNOWN),
        ("batStatus", "0", "normal"),
        ("batStatus", "1", "fault"),
        ("batStatus", "2", "disconnected"),
        ("batStatus", None, STATE_UNKNOWN),
    ],
)
async def test_status_codes_map_to_states(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    requests_mock: rm.Mocker,
    key: str,
    code: str | None,
    expected: str,
) -> None:
    """Status codes become named states; undocumented codes are unknown."""
    requests_mock.get(API_URL, json=ok_response(**{key: code}))

    await async_setup(hass, config_entry)

    assert state(hass, key) == expected
