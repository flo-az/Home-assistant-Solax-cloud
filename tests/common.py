"""Shared constants and helpers for the Solax Cloud tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.solax_cloud.const import DOMAIN

API_ADDRESS = "https://global.solaxcloud.com"
API_PATH = "/api/v2/dataAccess/realtimeInfo/get"
API_URL = f"{API_ADDRESS}{API_PATH}"

TOKEN = "20240101000000000000000"
SERIAL = "SWTEST0001"
UNIQUE_ID = f"SolaxCloud_{SERIAL}"

# A v2 realtime result from a live X3-Hybrid-G4 (plant time zone UTC+01:00,
# CEST at the time), 2026-10-06. Only the serial numbers are replaced.
REALTIME_RESULT: dict[str, Any] = {
    "inverterSN": "H3TEST0000001",
    "sn": SERIAL,
    "acpower": 3433.0,
    "yieldtoday": 12.6,
    "yieldtotal": 24215.1,
    "feedinpower": 2860.0,
    "feedinenergy": 14401.6,
    "consumeenergy": 6818.35,
    "feedinpowerM2": 0.0,
    "soc": 100.0,
    "peps1": 0.0,
    "peps2": 0.0,
    "peps3": 0.0,
    "inverterType": "14",
    "inverterStatus": "102",
    "uploadTime": "2026-10-06 15:23:04",
    "batPower": -17.0,
    "powerdc1": 2181.0,
    "powerdc2": 1235.0,
    "powerdc3": None,
    "powerdc4": None,
    "batStatus": "0",
    "utcDateTime": "2026-10-06T13:23:04Z",
}


# 40 s after the sample's upload (utcDateTime), when its data is current.
LIVE_SAMPLE_NOW = datetime(2026, 10, 6, 13, 23, 44, tzinfo=UTC)


def ok_response(**overrides: Any) -> dict[str, Any]:
    """A successful API response, optionally with some result fields replaced."""
    return {
        "success": True,
        "exception": "operation success",
        "result": {**REALTIME_RESULT, **overrides},
        "code": 0,
    }


# Refusals as returned by the live API (HTTP 200, success=false).
TOKEN_REJECTED: dict[str, Any] = {
    "exception": "token invalid!",
    "code": 103,
    "tokenId": TOKEN,
    "success": False,
}
SERIAL_REJECTED: dict[str, Any] = {
    "success": False,
    "exception": "no auth!",
    "result": None,
    "code": 1003,
}
# SolaXCloud User API V1.2, 8.2 "Error Code".
OPERATION_FAILED: dict[str, Any] = {
    "success": False,
    "exception": "Operation failed",
    "result": None,
    "code": 2001,
}

CONNECTION_FAILURES = [
    pytest.param({"exc": aiohttp.ClientConnectionError()}, id="network-down"),
    pytest.param({"exc": TimeoutError()}, id="cloud-too-slow"),
    pytest.param({"status": 503}, id="http-5xx"),
    pytest.param({"status": 403}, id="http-4xx"),
    pytest.param(
        {"text": "<html><body>Maintenance</body></html>"},
        id="maintenance-page-instead-of-json",
    ),
]
SERVICE_FAILURES = [
    *CONNECTION_FAILURES,
    pytest.param({"json": OPERATION_FAILED}, id="operation-failed"),
]
AUTH_FAILURES = [
    pytest.param({"json": TOKEN_REJECTED}, id="token-rejected"),
    pytest.param({"json": SERIAL_REJECTED}, id="serial-not-in-account"),
]


def respond(
    aioclient_mock: AiohttpClientMocker, url: str = API_URL, **response: Any
) -> None:
    """Make the API answer every following request with this response."""
    aioclient_mock.clear_requests()
    aioclient_mock.post(url, **response)


async def async_setup(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Set up a config entry the way Home Assistant does at boot."""
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def async_poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Advance time to the coordinator's next poll and let it run."""
    entries = hass.config_entries.async_entries(DOMAIN)
    interval = timedelta(seconds=61)
    if entries and entries[0].state is ConfigEntryState.LOADED:
        interval = entries[0].runtime_data.update_interval + timedelta(seconds=1)
    freezer.tick(interval)
    async_fire_time_changed(hass)
    # Scheduled refreshes run as config entry background tasks.
    await hass.async_block_till_done(wait_background_tasks=True)


def entity_id(hass: HomeAssistant, key: str) -> str:
    """Entity id of the sensor for an API key, found by its unique id.

    Unique ids must never change: users' entity ids and history hang off them.
    """
    found = er.async_get(hass).async_get_entity_id(
        SENSOR_DOMAIN, DOMAIN, f"{UNIQUE_ID}_test_{key}"
    )
    assert found is not None, f"no sensor registered for {key!r}"
    return found


def state(hass: HomeAssistant, key: str) -> str:
    """Current state string of the sensor for an API key."""
    current = hass.states.get(entity_id(hass, key))
    assert current is not None
    return current.state
