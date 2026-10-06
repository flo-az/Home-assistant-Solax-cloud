"""Tests that every text the config flow shows has an English translation."""

from __future__ import annotations

import json
from pathlib import Path

import requests
import requests_mock as rm

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_get_translations
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solax_cloud.const import CONF_SERIAL, CONF_TOKEN, DOMAIN

from .common import (
    API_URL,
    REJECTED_RESPONSE,
    SERIAL,
    TOKEN,
    async_setup,
    entity_id,
)

INTEGRATION_DIR = Path(__file__).parents[1] / "custom_components" / DOMAIN
PREFIX = f"component.{DOMAIN}.config"


async def _config_strings(hass: HomeAssistant) -> dict[str, str]:
    return await async_get_translations(hass, "en", "config", {DOMAIN})


async def test_regression_form_fields_have_labels(hass: HomeAssistant) -> None:
    """Regression: the token and serial fields were shown without labels.

    Custom integrations are translated from translations/en.json only
    (strings.json is compiled for core integrations), and the labels that did
    exist were keyed "token"/"serial" instead of the form's field names.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    fields = [str(marker) for marker in result["data_schema"].schema]
    strings = await _config_strings(hass)

    assert fields == [CONF_TOKEN, CONF_SERIAL]
    missing = [f for f in fields if not strings.get(f"{PREFIX}.step.user.data.{f}")]
    assert missing == []
    assert strings.get(f"{PREFIX}.step.user.title")


async def test_every_flow_error_and_abort_has_text(
    hass: HomeAssistant, requests_mock: rm.Mocker
) -> None:
    """Each error and abort reason the flow can return is translated."""
    user_input = {CONF_TOKEN: TOKEN, CONF_SERIAL: SERIAL}
    flows = hass.config_entries.flow
    reasons: list[str] = []

    first = await flows.async_init(DOMAIN, context={"source": SOURCE_USER})
    for response in (
        {"exc": requests.exceptions.ConnectionError},
        {"json": REJECTED_RESPONSE},
    ):
        requests_mock.get(API_URL, **response)
        result = await flows.async_configure(first["flow_id"], user_input)
        reasons.append(f"error.{result['errors']['base']}")

    # A second dialog for the same dongle while the first is still open.
    second = await flows.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await flows.async_configure(second["flow_id"], user_input)
    reasons.append(f"abort.{result['reason']}")
    flows.async_abort(first["flow_id"])

    MockConfigEntry(domain=DOMAIN, unique_id=f"SolaxCloud_{SERIAL}").add_to_hass(hass)
    third = await flows.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await flows.async_configure(third["flow_id"], user_input)
    reasons.append(f"abort.{result['reason']}")

    strings = await _config_strings(hass)
    assert reasons == [
        "error.cannot_connect",
        "error.invalid_token_or_serial",
        "abort.already_in_progress",
        "abort.already_configured",
    ]
    assert [r for r in reasons if not strings.get(f"{PREFIX}.{r}")] == []


async def test_translations_contain_no_unresolved_references(
    hass: HomeAssistant,
) -> None:
    """[%key:...%] references are only resolved for core integrations.

    In a custom integration they would be shown to the user verbatim.
    """
    strings = await _config_strings(hass)

    assert strings
    assert {k: v for k, v in strings.items() if "[%key:" in v} == {}


def test_strings_json_matches_english_translation() -> None:
    """strings.json (used if the integration moves into core) stays in sync."""
    strings = json.loads((INTEGRATION_DIR / "strings.json").read_text())
    english = json.loads((INTEGRATION_DIR / "translations" / "en.json").read_text())

    assert strings == english


async def test_every_status_state_has_text(
    hass: HomeAssistant, config_entry: MockConfigEntry, solax_api: rm.Mocker
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
