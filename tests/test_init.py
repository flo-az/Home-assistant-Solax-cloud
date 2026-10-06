"""Tests for setting up and unloading the Solax Cloud config entry."""

from __future__ import annotations

from typing import Any

import aiohttp
from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.solax_cloud.const import (
    CONF_API_ADDRESS,
    CONF_SERIAL,
    CONF_TOKEN,
    DOMAIN,
)

from .common import (
    API_URL,
    AUTH_FAILURES,
    SERIAL,
    SERVICE_FAILURES,
    TOKEN,
    UNIQUE_ID,
    async_poll,
    async_setup,
    ok_response,
    respond,
    state,
)


async def test_setup_publishes_live_payload(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    solax_api: AiohttpClientMocker,
) -> None:
    """Every field the API returns shows up on its sensor."""
    await async_setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    expected = {
        "inverterSN": "H3TEST0000001",
        "sn": SERIAL,
        "inverterStatus": "normal",
        "acpower": "3433.0",
        "yieldtoday": "12.6",
        "yieldtotal": "24215.1",
        "feedinpower": "2860.0",
        "feedinenergy": "14401.6",
        "consumeenergy": "6818.35",
        "soc": "100.0",
        "batStatus": "normal",
        "peps1": "0.0",
        "batPower": "-17.0",
        "powerdc1": "2181.0",
        "powerdc2": "1235.0",
        "total_solar_power": "3416.0",
        "uploadTime": "2026-10-06 15:23:04",
        "utcDateTime": "2026-10-06T13:23:04+00:00",
    }
    assert {key: state(hass, key) for key in expected} == expected


async def test_setup_sends_one_v2_request(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    solax_api: AiohttpClientMocker,
) -> None:
    """Setup spends one request: a v2 POST with the token in a header."""
    await async_setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    [(method, url, body, headers)] = solax_api.mock_calls
    assert (method.upper(), str(url)) == ("POST", API_URL)
    assert body == {"wifiSn": SERIAL}
    assert headers["tokenId"] == TOKEN


async def test_entry_from_v1_uses_global_api_address(
    hass: HomeAssistant, solax_api: AiohttpClientMocker
) -> None:
    """Entries created before the v2 switch have no API address stored."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=SERIAL,
        unique_id=UNIQUE_ID,
        data={CONF_TOKEN: TOKEN, CONF_SERIAL: SERIAL},
    )
    entry.add_to_hass(hass)

    await async_setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert str(solax_api.mock_calls[0][1]) == API_URL


@pytest.mark.parametrize("response", SERVICE_FAILURES)
async def test_regression_failed_request_at_boot_is_retried(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    response: dict[str, Any],
) -> None:
    """Regression: a failed request during setup left the entry dead.

    Booting while the network was down (or the cloud slow, erroring or
    refusing the request) raised out of async_setup_entry, so Home Assistant
    marked the entry SETUP_ERROR and never retried. A refused request also
    filed a repair issue claiming the token was invalid.
    """
    respond(aioclient_mock, **response)

    await async_setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert ir.async_get(hass).issues == {}


@pytest.mark.parametrize("response", AUTH_FAILURES)
async def test_rejected_credentials_at_boot_ask_for_new_token(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    response: dict[str, Any],
) -> None:
    """A token v2 refuses (e.g. a v1-only token) starts re-authentication."""
    respond(aioclient_mock, **response)

    await async_setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = config_entry.async_get_active_flows(hass, {SOURCE_REAUTH})
    assert [flow["step_id"] for flow in flows] == ["reauth_confirm"]


async def test_setup_retry_recovers_once_cloud_answers(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """After a failed boot the entry loads by itself once the cloud is back."""
    respond(aioclient_mock, exc=aiohttp.ClientConnectionError())
    await async_setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY

    respond(aioclient_mock, json=ok_response())
    await async_poll(hass, freezer)

    assert config_entry.state is ConfigEntryState.LOADED
    assert state(hass, "acpower") == "3433.0"


async def test_unload(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    solax_api: AiohttpClientMocker,
) -> None:
    """Unloading removes the entry's sensors from service."""
    await async_setup(hass, config_entry)

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.NOT_LOADED
    assert state(hass, "acpower") == "unavailable"


@pytest.mark.parametrize("code", [[], {}, "103", 103.5, True], ids=repr)
async def test_regression_refusal_with_odd_code_is_an_ordinary_error(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    code: Any,
) -> None:
    """Regression: a refusal whose code was a list or object raised TypeError.

    The code was looked up in a frozenset, which needs a hashable value; the
    error escaped as "Unexpected error fetching solax_cloud data". Found as
    a rare property test failure.
    """
    respond(aioclient_mock, json={"success": False, "code": code})

    await async_setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert "Unexpected error" not in caplog.text


async def test_stored_plain_http_address_is_refused(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An entry stored with http:// never sends the token unencrypted."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=SERIAL,
        unique_id=UNIQUE_ID,
        data={
            CONF_API_ADDRESS: "http://global.solaxcloud.com",
            CONF_TOKEN: TOKEN,
            CONF_SERIAL: SERIAL,
        },
    )
    entry.add_to_hass(hass)

    await async_setup(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.error_reason_translation_key == "insecure_address"
    assert aioclient_mock.call_count == 0
