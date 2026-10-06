"""Tests for the Solax Cloud sensors while the integration is running."""

from __future__ import annotations

from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from .common import (
    AUTH_FAILURES,
    OPERATION_FAILED,
    SERVICE_FAILURES,
    async_poll,
    async_setup,
    ok_response,
    respond,
    state,
)


@pytest.mark.parametrize("response", [*SERVICE_FAILURES, *AUTH_FAILURES])
async def test_regression_failed_poll_marks_sensors_unavailable(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    solax_api: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    response: dict[str, Any],
) -> None:
    """Regression: a refused poll froze every sensor on its last value.

    The v1 client returned None for success=false (token revoked, request
    refused). The coordinator stored that as fresh data, every sensor raised
    AttributeError on None.get() and kept showing the previous reading as if
    it were current.
    """
    await async_setup(hass, config_entry)
    assert state(hass, "acpower") == "3433.0"

    respond(solax_api, **response)
    await async_poll(hass, freezer)

    assert state(hass, "acpower") == STATE_UNAVAILABLE
    assert state(hass, "total_solar_power") == STATE_UNAVAILABLE
    assert "AttributeError" not in caplog.text


async def test_sensors_recover_after_failed_poll(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    solax_api: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The next good poll brings sensors back with the new readings."""
    await async_setup(hass, config_entry)
    respond(solax_api, json=OPERATION_FAILED)
    await async_poll(hass, freezer)
    assert state(hass, "acpower") == STATE_UNAVAILABLE

    respond(solax_api, json=ok_response(acpower=1000.0))
    await async_poll(hass, freezer)

    assert state(hass, "acpower") == "1000.0"


async def test_missing_reading_is_unknown(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A null field (e.g. no battery) leaves its sensor unknown, not zero."""
    respond(aioclient_mock, json=ok_response(soc=None, batPower=None))

    await async_setup(hass, config_entry)

    assert state(hass, "soc") == STATE_UNKNOWN
    assert state(hass, "batPower") == STATE_UNKNOWN


async def test_implausible_numbers_are_unknown(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Readings no plant produces never reach a state."""
    respond(
        aioclient_mock,
        json=ok_response(
            acpower=1.0e300, soc=-(10**18), powerdc1=1.0e308, powerdc2=1.0e308
        ),
    )

    await async_setup(hass, config_entry)

    assert [state(hass, k) for k in ("acpower", "soc", "total_solar_power")] == [
        STATE_UNKNOWN
    ] * 3
    assert "ERROR" not in caplog.text


@pytest.mark.parametrize(
    "reading",
    [True, False, "3433.0", [3433.0], {"value": 3433.0}],
    ids=["true", "false", "numeric-string", "list", "object"],
)
async def test_wrongly_typed_readings_are_unknown(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    reading: Any,
) -> None:
    """A reading that is not a JSON number (as documented) is unknown."""
    respond(aioclient_mock, json=ok_response(acpower=reading))

    await async_setup(hass, config_entry)

    assert state(hass, "acpower") == STATE_UNKNOWN
    assert "ERROR" not in caplog.text


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
async def test_non_standard_json_fails_the_poll(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    token: str,
) -> None:
    """NaN/Infinity are not JSON; such a body is a failed request."""
    respond(
        aioclient_mock,
        text=f'{{"success": true, "code": 0, "result": {{"acpower": {token}}}}}',
    )

    await async_setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.parametrize("time_zone", ["Europe/Berlin", "US/Pacific", "UTC"])
async def test_utc_date_time_is_the_upload_instant(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    solax_api: AiohttpClientMocker,
    time_zone: str,
) -> None:
    """v2 sends utcDateTime in real UTC; no correction may be applied.

    v1 sent the plant's wall time read as UTC+8, which earlier versions
    corrected by a fixed offset. The live v2 sample was uploaded at
    15:23:04 CEST, i.e. 13:23:04 UTC, whatever zone Home Assistant runs in.
    """
    await hass.config.async_set_time_zone(time_zone)

    await async_setup(hass, config_entry)

    assert state(hass, "utcDateTime") == "2026-10-06T13:23:04+00:00"


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
    aioclient_mock: AiohttpClientMocker,
    key: str,
    code: str | None,
    expected: str,
) -> None:
    """Status codes become named states; undocumented codes are unknown."""
    respond(aioclient_mock, json=ok_response(**{key: code}))

    await async_setup(hass, config_entry)

    assert state(hass, key) == expected
