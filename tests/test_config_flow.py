"""Tests for the Solax Cloud config flow."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import requests_mock as rm

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solax_cloud.const import CONF_SERIAL, CONF_TOKEN, DOMAIN

from .common import (
    API_URL,
    CONNECTION_FAILURES,
    REJECTED_RESPONSE,
    SERIAL,
    TOKEN,
    UNIQUE_ID,
    ok_response,
)

USER_INPUT = {CONF_TOKEN: TOKEN, CONF_SERIAL: SERIAL}


async def _start(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def _submit(
    hass: HomeAssistant, flow: dict[str, Any], user_input: dict[str, str]
) -> dict[str, Any]:
    return await hass.config_entries.flow.async_configure(flow["flow_id"], user_input)


async def test_creates_entry_for_valid_credentials(
    hass: HomeAssistant, solax_api: rm.Mocker
) -> None:
    """Valid credentials create an entry named after the dongle serial."""
    result = await _submit(hass, await _start(hass), USER_INPUT)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == SERIAL
    assert result["data"] == USER_INPUT
    assert result["result"].unique_id == UNIQUE_ID
    query = parse_qs(urlparse(solax_api.last_request.url).query)
    assert query == {"tokenId": [TOKEN], "sn": [SERIAL]}


@pytest.mark.parametrize("response", CONNECTION_FAILURES)
async def test_regression_unreachable_cloud_shows_cannot_connect(
    hass: HomeAssistant, requests_mock: rm.Mocker, response: dict[str, Any]
) -> None:
    """Regression: most network failures crashed the flow with "Unknown error".

    Only ConnectTimeout and HTTPError were caught, but solaxcloud surfaces a
    DNS failure or slow cloud as ConnectionError, exhausted 5xx retries as
    RetryError and a non-JSON body as JSONDecodeError.
    """
    requests_mock.get(API_URL, **response)

    result = await _submit(hass, await _start(hass), USER_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_rejected_credentials_show_error_and_can_be_corrected(
    hass: HomeAssistant, requests_mock: rm.Mocker
) -> None:
    """A refused token/serial keeps the form open so the user can fix it."""
    requests_mock.get(API_URL, json=REJECTED_RESPONSE)
    flow = await _start(hass)

    result = await _submit(hass, flow, USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_token_or_serial"}

    requests_mock.get(API_URL, json=ok_response())
    result = await _submit(hass, flow, USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    "serial",
    [SERIAL, f" {SERIAL}", f"{SERIAL} ", f"\t{SERIAL}\n"],
    ids=["exact", "leading-space", "trailing-space", "tab-and-newline"],
)
async def test_regression_same_dongle_with_whitespace_is_a_duplicate(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    solax_api: rm.Mocker,
    serial: str,
) -> None:
    """Regression: a pasted serial with leading whitespace added a second entry.

    The unique id stripped the formatted string, not the serial, so
    "SolaxCloud_ SW..." did not match the existing "SolaxCloud_SW...".
    """
    result = await _submit(
        hass, await _start(hass), {CONF_TOKEN: TOKEN, CONF_SERIAL: serial}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert solax_api.call_count == 0
