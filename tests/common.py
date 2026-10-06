"""Shared constants and helpers for the Solax Cloud tests."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
import requests

from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.solax_cloud.const import DOMAIN

API_URL = "https://www.solaxcloud.com/proxyApp/proxy/api/getRealtimeInfo.do"

TOKEN = "20240101000000000000000"
SERIAL = "SWTEST0001"
UNIQUE_ID = f"SolaxCloud_{SERIAL}"

# A getRealtimeInfo.do result as returned for a live X3-Hybrid-G4 on 2026-10-06
# (plant time zone UTC+01:00 with DST, so CEST at the time). Serial numbers are
# replaced; inverterSN, feedinpowerM2, inverterType, inverterStatus and batStatus
# are not read by the integration and hold placeholder values.
REALTIME_RESULT: dict[str, Any] = {
    "inverterSN": "H3TEST0000001",
    "sn": SERIAL,
    "acpower": 2224.0,
    "yieldtoday": 9.2,
    "yieldtotal": 24211.7,
    "feedinpower": 1593.0,
    "feedinenergy": 14398.6,
    "consumeenergy": 6818.35,
    "feedinpowerM2": 0.0,
    "soc": 97.0,
    "peps1": 0.0,
    "peps2": 0.0,
    "peps3": 0.0,
    "inverterType": "14",
    "inverterStatus": "102",
    "uploadTime": "2026-10-06 14:33:04",
    "utcDateTime": "2026-10-06T06:33:04Z",
    "batPower": 3007.0,
    "powerdc1": 3365.0,
    "powerdc2": 1866.0,
    "powerdc3": None,
    "powerdc4": None,
    "batStatus": "0",
}


def ok_response(**overrides: Any) -> dict[str, Any]:
    """A successful API response, optionally with some result fields replaced."""
    return {
        "success": True,
        "exception": "Query success!",
        "result": {**REALTIME_RESULT, **overrides},
        "code": 0,
    }


# The API answers HTTP 200 with success=false when it refuses a request
# (SolaXCloud User API V1.2, 8.2 "Error Code"): an unknown token is 1001.
REJECTED_RESPONSE: dict[str, Any] = {
    "success": False,
    "exception": "Interface Unauthorized",
    "result": None,
    "code": 1001,
}

# What solaxcloud 0.1.0 surfaces for each transport failure. requests_mock
# bypasses the library's urllib3 retry adapter, so retried failures are raised
# here as the exceptions the adapter ends with once its retries are exhausted.
CONNECTION_FAILURES = [
    pytest.param(
        {"exc": requests.exceptions.ConnectionError},
        id="network-down-or-cloud-too-slow",
    ),
    pytest.param({"exc": requests.exceptions.RetryError}, id="cloud-5xx-after-retries"),
    pytest.param({"status_code": 403}, id="http-4xx"),
    pytest.param(
        {"status_code": 200, "text": "<html><body>Maintenance</body></html>"},
        id="maintenance-page-instead-of-json",
    ),
]

FAILED_REQUESTS = [
    *CONNECTION_FAILURES,
    pytest.param({"json": REJECTED_RESPONSE}, id="request-rejected"),
]


async def async_setup(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Set up a config entry the way Home Assistant does at boot."""
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def async_poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Advance time so the coordinator's polling timer fires once."""
    freezer.tick(timedelta(seconds=61))
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
