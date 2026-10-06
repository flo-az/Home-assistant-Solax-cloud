"""Tests that every text the integration shows has an English translation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_get_translations
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
    OPERATION_FAILED,
    SERIAL,
    SERIAL_REJECTED,
    TOKEN,
    TOKEN_REJECTED,
    async_setup,
    entity_id,
    ok_response,
    respond,
)

INTEGRATION_DIR = Path(__file__).parents[1] / "custom_components" / DOMAIN
PREFIX = f"component.{DOMAIN}.config"
USER_INPUT = {CONF_API_ADDRESS: API_ADDRESS, CONF_TOKEN: TOKEN, CONF_SERIAL: SERIAL}


async def _config_strings(hass: HomeAssistant) -> dict[str, str]:
    return await async_get_translations(hass, "en", "config", {DOMAIN})


def _missing_labels(form: dict[str, Any], strings: dict[str, str]) -> list[str]:
    step = f"{PREFIX}.step.{form['step_id']}"
    fields = [str(marker) for marker in form["data_schema"].schema]
    missing = [f for f in fields if not strings.get(f"{step}.data.{f}")]
    if not strings.get(f"{step}.title"):
        missing.append("title")
    return missing


async def test_regression_form_fields_have_labels(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Regression: the token and serial fields were shown without labels.

    Custom integrations are translated from translations/en.json only
    (strings.json is compiled for core integrations), and the labels that did
    exist were keyed "token"/"serial" instead of the form's field names.
    Covers the setup form and the re-authentication form.
    """
    strings = await _config_strings(hass)
    user_form = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    respond(aioclient_mock, json=TOKEN_REJECTED)
    await async_setup(hass, config_entry)
    [reauth_form] = config_entry.async_get_active_flows(hass, {"reauth"})
    reauth_form = await hass.config_entries.flow.async_configure(reauth_form["flow_id"])

    reconfigure_form = await config_entry.start_reconfigure_flow(hass)

    assert {
        "user": _missing_labels(user_form, strings),
        "reauth_confirm": _missing_labels(reauth_form, strings),
        "reconfigure": _missing_labels(reconfigure_form, strings),
    } == {"user": [], "reauth_confirm": [], "reconfigure": []}


@pytest.mark.usefixtures("mock_setup_entry")
async def test_every_flow_error_and_abort_has_text(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Each error and abort reason the setup flow can return is translated."""
    flows = hass.config_entries.flow
    reasons: list[str] = []

    first = await flows.async_init(DOMAIN, context={"source": SOURCE_USER})
    for response in (
        {"exc": aiohttp.ClientConnectionError()},
        {"json": TOKEN_REJECTED},
        {"json": SERIAL_REJECTED},
        {"json": OPERATION_FAILED},
    ):
        respond(aioclient_mock, **response)
        result = await flows.async_configure(first["flow_id"], USER_INPUT)
        reasons.append(f"error.{result['errors']['base']}")

    result = await flows.async_configure(
        first["flow_id"],
        {**USER_INPUT, CONF_API_ADDRESS: "http://global.solaxcloud.com"},
    )
    reasons.append(f"error.{result['errors']['base']}")

    # A second dialog for the same dongle while the first is still open.
    second = await flows.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await flows.async_configure(second["flow_id"], USER_INPUT)
    reasons.append(f"abort.{result['reason']}")
    flows.async_abort(first["flow_id"])

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=f"SolaxCloud_{SERIAL}", data=USER_INPUT
    )
    entry.add_to_hass(hass)
    third = await flows.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await flows.async_configure(third["flow_id"], USER_INPUT)
    reasons.append(f"abort.{result['reason']}")

    respond(aioclient_mock, json=ok_response())
    reauth = await entry.start_reauth_flow(hass)
    result = await flows.async_configure(
        reauth["flow_id"], {CONF_API_ADDRESS: API_ADDRESS, CONF_TOKEN: TOKEN}
    )
    reasons.append(f"abort.{result['reason']}")

    strings = await _config_strings(hass)
    assert reasons == [
        "error.cannot_connect",
        "error.invalid_token",
        "error.serial_not_in_account",
        "error.api_error",
        "error.insecure_address",
        "abort.already_in_progress",
        "abort.already_configured",
        "abort.reauth_successful",
    ]
    assert [r for r in reasons if not strings.get(f"{PREFIX}.{r}")] == []


async def test_translations_contain_no_unresolved_references(
    hass: HomeAssistant,
) -> None:
    """[%key:...%] references are only resolved for core integrations.

    In a custom integration they would be shown to the user verbatim.
    """
    strings = {
        **await _config_strings(hass),
        **await async_get_translations(hass, "en", "entity", {DOMAIN}),
    }

    assert strings
    assert {k: v for k, v in strings.items() if "[%key:" in v} == {}


def test_strings_json_matches_english_translation() -> None:
    """strings.json (used if the integration moves into core) stays in sync."""
    strings = json.loads((INTEGRATION_DIR / "strings.json").read_text())
    english = json.loads((INTEGRATION_DIR / "translations" / "en.json").read_text())

    assert strings == english


async def test_every_status_state_has_text(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    solax_api: AiohttpClientMocker,
) -> None:
    """Each state a status sensor can take has an English name."""
    await async_setup(hass, config_entry)
    strings = await async_get_translations(hass, "en", "entity", {DOMAIN})

    missing = []
    for key in ("inverterStatus", "batStatus"):
        entry = er.async_get(hass).async_get(entity_id(hass, key))
        options = hass.states.get(entry.entity_id).attributes["options"]
        assert options
        prefix = f"component.{DOMAIN}.entity.sensor.{entry.translation_key}.state"
        missing += [o for o in options if not strings.get(f"{prefix}.{o}")]
    assert missing == []
