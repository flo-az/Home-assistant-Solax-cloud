"""Tests for the Solax Cloud config flow."""

from __future__ import annotations

from typing import Any

import pytest

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
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
    API_ADDRESS,
    API_PATH,
    API_URL,
    CONNECTION_FAILURES,
    OPERATION_FAILED,
    SERIAL,
    SERIAL_REJECTED,
    TOKEN,
    TOKEN_REJECTED,
    UNIQUE_ID,
    async_setup,
    ok_response,
    respond,
)

USER_INPUT = {CONF_API_ADDRESS: API_ADDRESS, CONF_TOKEN: TOKEN, CONF_SERIAL: SERIAL}
NEW_TOKEN = "20260101000000000000000"


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


@pytest.mark.usefixtures("mock_setup_entry")
async def test_creates_entry_for_valid_credentials(
    hass: HomeAssistant, solax_api: AiohttpClientMocker
) -> None:
    """Valid credentials create an entry named after the dongle serial."""
    result = await _submit(hass, await _start(hass), USER_INPUT)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == SERIAL
    assert result["data"] == USER_INPUT
    assert result["result"].unique_id == UNIQUE_ID
    [(_, url, body, headers)] = solax_api.mock_calls
    assert (str(url), body, headers["tokenId"]) == (API_URL, {"wifiSn": SERIAL}, TOKEN)


@pytest.mark.parametrize(
    ("entered", "stored"),
    [
        ("https://euapi.solaxcloud.com", "https://euapi.solaxcloud.com"),
        ("https://euapi.solaxcloud.com/", "https://euapi.solaxcloud.com"),
        ("euapi.solaxcloud.com", "https://euapi.solaxcloud.com"),
        (" https://euapi.solaxcloud.com ", "https://euapi.solaxcloud.com"),
    ],
    ids=["as-shown", "trailing-slash", "no-scheme", "padded"],
)
@pytest.mark.usefixtures("mock_setup_entry")
async def test_uses_the_accounts_api_address(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    entered: str,
    stored: str,
) -> None:
    """The address from the account's API page is used, however it is pasted."""
    respond(aioclient_mock, url=f"{stored}{API_PATH}", json=ok_response())

    result = await _submit(
        hass, await _start(hass), {**USER_INPUT, CONF_API_ADDRESS: entered}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_API_ADDRESS] == stored


@pytest.mark.parametrize("response", CONNECTION_FAILURES)
async def test_regression_unreachable_cloud_shows_cannot_connect(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, response: dict[str, Any]
) -> None:
    """Regression: most network failures crashed the flow with "Unknown error".

    Only some transport errors were caught; a DNS failure, a slow cloud,
    exhausted 5xx retries and a non-JSON body escaped.
    """
    respond(aioclient_mock, **response)

    result = await _submit(hass, await _start(hass), USER_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_api_error_is_reported(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A refusal that is not about the credentials gets its own message."""
    respond(aioclient_mock, json=OPERATION_FAILED)

    result = await _submit(hass, await _start(hass), USER_INPUT)

    assert result["errors"] == {"base": "api_error"}


@pytest.mark.parametrize(
    ("response", "error"),
    [
        ({"json": TOKEN_REJECTED}, "invalid_token"),
        ({"json": SERIAL_REJECTED}, "serial_not_in_account"),
    ],
    ids=["token-rejected", "serial-not-in-account"],
)
@pytest.mark.usefixtures("mock_setup_entry")
async def test_rejected_credentials_show_error_and_can_be_corrected(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    response: dict[str, Any],
    error: str,
) -> None:
    """A refused token or serial says which, and the form stays open."""
    respond(aioclient_mock, **response)
    flow = await _start(hass)

    result = await _submit(hass, flow, USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    respond(aioclient_mock, json=ok_response())
    result = await _submit(hass, flow, USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_dongle_without_data_is_reported(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """success=true without a result (e.g. a new dongle) is an API error."""
    respond(
        aioclient_mock,
        json={
            "success": True,
            "exception": "operation success",
            "result": None,
            "code": 0,
        },
    )

    result = await _submit(hass, await _start(hass), USER_INPUT)

    assert result["errors"] == {"base": "api_error"}


@pytest.mark.parametrize(
    "serial",
    [SERIAL, f" {SERIAL}", f"{SERIAL} ", f"\t{SERIAL}\n"],
    ids=["exact", "leading-space", "trailing-space", "tab-and-newline"],
)
async def test_regression_same_dongle_with_whitespace_is_a_duplicate(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    solax_api: AiohttpClientMocker,
    serial: str,
) -> None:
    """Regression: a pasted serial with leading whitespace added a second entry.

    The unique id stripped the formatted string, not the serial, so
    "SolaxCloud_ SW..." did not match the existing "SolaxCloud_SW...".
    """
    result = await _submit(
        hass, await _start(hass), {**USER_INPUT, CONF_SERIAL: serial}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert solax_api.call_count == 0


async def test_reauth_replaces_token_and_reloads(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """After v2 refuses the stored token, a new one brings the entry back."""
    respond(aioclient_mock, json=TOKEN_REJECTED)
    await async_setup(hass, config_entry)
    [flow] = config_entry.async_get_active_flows(hass, {"reauth"})

    respond(aioclient_mock, json=ok_response())
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_API_ADDRESS: API_ADDRESS, CONF_TOKEN: NEW_TOKEN}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_TOKEN] == NEW_TOKEN
    assert config_entry.state is ConfigEntryState.LOADED
    assert aioclient_mock.mock_calls[-1][3]["tokenId"] == NEW_TOKEN


@pytest.mark.parametrize(
    ("response", "error"),
    [
        ({"json": TOKEN_REJECTED}, "invalid_token"),
        ({"json": SERIAL_REJECTED}, "serial_not_in_account"),
        ({"status": 503}, "cannot_connect"),
        ({"json": OPERATION_FAILED}, "api_error"),
    ],
)
async def test_reauth_keeps_form_open_on_failure(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    response: dict[str, Any],
    error: str,
) -> None:
    """A token that still fails is reported and the old one is kept."""
    respond(aioclient_mock, json=TOKEN_REJECTED)
    await async_setup(hass, config_entry)
    [flow] = config_entry.async_get_active_flows(hass, {"reauth"})

    respond(aioclient_mock, **response)
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_API_ADDRESS: API_ADDRESS, CONF_TOKEN: NEW_TOKEN}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    assert config_entry.data[CONF_TOKEN] == TOKEN


@pytest.mark.usefixtures("mock_setup_entry")
async def test_reconfigure_changes_address_and_token(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The API address and token can be changed without waiting for a failure."""
    respond(
        aioclient_mock,
        url=f"https://euapi.solaxcloud.com{API_PATH}",
        json=ok_response(),
    )
    flow = await config_entry.start_reconfigure_flow(hass)
    assert flow["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        {CONF_API_ADDRESS: "euapi.solaxcloud.com/", CONF_TOKEN: NEW_TOKEN},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data == {
        CONF_API_ADDRESS: "https://euapi.solaxcloud.com",
        CONF_TOKEN: NEW_TOKEN,
        CONF_SERIAL: SERIAL,
    }


@pytest.mark.parametrize(
    ("response", "error"),
    [
        ({"json": TOKEN_REJECTED}, "invalid_token"),
        ({"json": SERIAL_REJECTED}, "serial_not_in_account"),
        ({"status": 503}, "cannot_connect"),
    ],
)
async def test_reconfigure_keeps_settings_on_failure(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    response: dict[str, Any],
    error: str,
) -> None:
    """Settings that do not work are reported and not saved."""
    respond(aioclient_mock, **response)
    flow = await config_entry.start_reconfigure_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_API_ADDRESS: API_ADDRESS, CONF_TOKEN: NEW_TOKEN}
    )

    assert result["errors"] == {"base": error}
    assert config_entry.data[CONF_TOKEN] == TOKEN


@pytest.mark.usefixtures("mock_setup_entry")
async def test_reconfigure_without_token_keeps_it(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Re-entering the same address (in any spelling) needs no token."""
    respond(aioclient_mock, json=ok_response())
    flow = await config_entry.start_reconfigure_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_API_ADDRESS: "HTTPS://global.solaxcloud.com/"}
    )

    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_TOKEN] == TOKEN
    assert aioclient_mock.mock_calls[-1][3]["tokenId"] == TOKEN


@pytest.mark.parametrize("step", ["user", "reconfigure"])
async def test_plain_http_address_is_refused(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    step: str,
) -> None:
    """http:// would send the token unencrypted; nothing is requested."""
    address = "http://global.solaxcloud.com"
    if step == "user":
        flow = await _start(hass)
        result = await _submit(
            hass,
            flow,
            {**USER_INPUT, CONF_SERIAL: "SWOTHER001", CONF_API_ADDRESS: address},
        )
    else:
        flow = await config_entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {CONF_API_ADDRESS: address, CONF_TOKEN: NEW_TOKEN}
        )

    assert result["errors"] == {"base": "insecure_address"}
    assert aioclient_mock.call_count == 0


@pytest.mark.parametrize("code", [[], {}], ids=["list", "object"])
async def test_regression_refusal_with_odd_code_shows_api_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, code: Any
) -> None:
    """Regression: such a refusal crashed the setup dialog."""
    respond(aioclient_mock, json={"success": False, "code": code})

    result = await _submit(hass, await _start(hass), USER_INPUT)

    assert result["errors"] == {"base": "api_error"}


@pytest.mark.usefixtures("mock_setup_entry")
async def test_uppercase_https_is_accepted(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """HTTPS:// is https; the stored address is normalized to lowercase scheme."""
    respond(aioclient_mock, json=ok_response())

    result = await _submit(
        hass,
        await _start(hass),
        {**USER_INPUT, CONF_API_ADDRESS: "HTTPS://global.solaxcloud.com"},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_API_ADDRESS] == API_ADDRESS


async def test_reconfigure_to_another_host_needs_the_token(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The stored token is not sent to a different host without asking."""
    flow = await config_entry.start_reconfigure_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_API_ADDRESS: "https://other.example.com"}
    )

    assert result["errors"] == {"base": "token_required"}
    assert aioclient_mock.call_count == 0
