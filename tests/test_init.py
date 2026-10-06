"""Tests for setting up and unloading the Solax Cloud config entry."""

from __future__ import annotations

from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
import requests
import requests_mock as rm

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .common import (
    API_URL,
    FAILED_REQUESTS,
    SERIAL,
    async_poll,
    async_setup,
    ok_response,
    state,
)


async def test_setup_publishes_live_payload(
    hass: HomeAssistant, config_entry: MockConfigEntry, solax_api: rm.Mocker
) -> None:
    """Every field the API returns shows up on its sensor."""
    await async_setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    # Values as shown by a live Home Assistant install for this payload.
    expected = {
        "sn": SERIAL,
        "acpower": "2224.0",
        "yieldtoday": "9.2",
        "yieldtotal": "24211.7",
        "feedinpower": "1593.0",
        "feedinenergy": "14398.6",
        "consumeenergy": "6818.35",
        "soc": "97.0",
        "peps1": "0.0",
        "batPower": "3007.0",
        "powerdc1": "3365.0",
        "powerdc2": "1866.0",
        "total_solar_power": "5231.0",
        "uploadTime": "2026-10-06 14:33:04",
    }
    assert {key: state(hass, key) for key in expected} == expected


async def test_setup_makes_a_single_api_call(
    hass: HomeAssistant, config_entry: MockConfigEntry, solax_api: rm.Mocker
) -> None:
    """Setup spends one request of the token's rate limit, not two."""
    await async_setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    assert solax_api.call_count == 1


@pytest.mark.parametrize("response", FAILED_REQUESTS)
async def test_regression_failed_request_at_boot_is_retried(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    requests_mock: rm.Mocker,
    response: dict[str, Any],
) -> None:
    """Regression: a failed request during setup left the entry dead.

    Booting while the network was down (or the cloud slow, erroring or
    refusing the request) raised out of async_setup_entry, so Home Assistant
    marked the entry SETUP_ERROR and never retried. A refused request also
    filed a repair issue claiming the token was invalid.
    """
    requests_mock.get(API_URL, **response)

    await async_setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert ir.async_get(hass).issues == {}


async def test_setup_retry_recovers_once_cloud_answers(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    requests_mock: rm.Mocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """After a failed boot the entry loads by itself once the cloud is back."""
    requests_mock.get(API_URL, exc=requests.exceptions.ConnectionError)
    await async_setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY

    requests_mock.get(API_URL, json=ok_response())
    await async_poll(hass, freezer)

    assert config_entry.state is ConfigEntryState.LOADED
    assert state(hass, "acpower") == "2224.0"


async def test_unload(
    hass: HomeAssistant, config_entry: MockConfigEntry, solax_api: rm.Mocker
) -> None:
    """Unloading removes the entry's sensors from service."""
    await async_setup(hass, config_entry)

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.NOT_LOADED
    assert state(hass, "acpower") == "unavailable"
