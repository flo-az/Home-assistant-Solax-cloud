"""Tests for diagnostics, entity categories, icons and translated errors."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp.client_reqrep import RequestInfo
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.icon import async_get_icons
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component
from multidict import CIMultiDict, CIMultiDictProxy
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator
from yarl import URL

from custom_components.solax_cloud.const import DOMAIN

from .common import (
    API_URL,
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


async def test_diagnostics_hide_token_and_serials(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    config_entry: MockConfigEntry,
    solax_api: AiohttpClientMocker,
) -> None:
    """Diagnostics show the data and state, never the token or serial numbers."""
    await async_setup(hass, config_entry)

    diagnostics = await get_diagnostics_for_config_entry(
        hass, hass_client, config_entry
    )

    text = json.dumps(diagnostics)
    assert TOKEN not in text
    assert SERIAL not in text
    assert "H3TEST0000001" not in text
    assert diagnostics["data"]["acpower"] == 3433.0
    assert diagnostics["data"]["utcDateTime"] == "2026-10-06T13:23:04Z"
    assert diagnostics["entry"]["api_address"] == "https://global.solaxcloud.com"
    assert diagnostics["polling"]["last_update_success"] is True
    assert diagnostics["polling"]["stale"] is False


async def test_metadata_sensors_are_diagnostic(
    hass: HomeAssistant, config_entry: MockConfigEntry, solax_api: AiohttpClientMocker
) -> None:
    """Serials and upload times go to the device page's diagnostic section."""
    await async_setup(hass, config_entry)
    registry = er.async_get(hass)

    diagnostic = {
        key
        for key in (
            "inverterSN",
            "sn",
            "uploadTime",
            "utcDateTime",
            "acpower",
            "soc",
            "inverterStatus",
            "battery_charge_energy",
        )
        if registry.async_get(entity_id(hass, key)).entity_category
        is EntityCategory.DIAGNOSTIC
    }
    assert diagnostic == {"inverterSN", "sn", "uploadTime", "utcDateTime"}


async def test_every_sensor_has_an_icon(
    hass: HomeAssistant, config_entry: MockConfigEntry, solax_api: AiohttpClientMocker
) -> None:
    """Icons distinguish the sensors; state of charge keeps the battery level icon."""
    await async_setup(hass, config_entry)
    icons = (await async_get_icons(hass, "entity", {DOMAIN}))[DOMAIN]["sensor"]

    entries = er.async_entries_for_config_entry(
        er.async_get(hass), config_entry.entry_id
    )
    without_icon = {
        e.translation_key
        for e in entries
        if not icons.get(e.translation_key, {}).get("default")
    }
    assert without_icon == {"soc"}


@pytest.mark.parametrize(
    ("response", "key", "german"),
    [
        (
            {"exc": aiohttp.ClientConnectionError()},
            "cannot_connect",
            "Keine Verbindung zu Solax Cloud",
        ),
        (
            {"json": OPERATION_FAILED},
            "api_error",
            "Solax Cloud konnte die Anfrage nicht verarbeiten",
        ),
        (
            {"json": TOKEN_REJECTED},
            "invalid_token",
            "Solax Cloud hat die Token-ID abgelehnt",
        ),
        (
            {"json": SERIAL_REJECTED},
            "serial_not_in_account",
            "gehört nicht zum Konto dieser Token-ID",
        ),
    ],
    ids=["connection", "api-error", "token", "serial"],
)
async def test_setup_errors_are_translated(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    response: dict[str, Any],
    key: str,
    german: str,
) -> None:
    """The integration card shows the reason in the user's language.

    The backend logs English; the frontend renders the reason from its
    translation key, so the key and a German text for it must exist.
    """
    respond(aioclient_mock, **response)

    await async_setup(hass, config_entry)

    assert config_entry.error_reason_translation_key == key
    texts = await async_get_translations(hass, "de", "exceptions", {DOMAIN})
    message = texts[f"component.{DOMAIN}.exceptions.{key}.message"]
    rendered = message.format(
        **(config_entry.error_reason_translation_placeholders or {})
    )
    assert german in rendered


def _flatten(tree: dict[str, Any], prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in tree.items():
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{prefix}{key}."))
        else:
            flat[f"{prefix}{key}"] = value
    return flat


def test_german_translation_matches_english() -> None:
    """Every English text has a German one with the same placeholders."""
    english = _flatten(
        json.loads((INTEGRATION_DIR / "translations" / "en.json").read_text())
    )
    german = _flatten(
        json.loads((INTEGRATION_DIR / "translations" / "de.json").read_text())
    )

    assert german.keys() == english.keys()
    placeholders = {
        key: (set(re.findall(r"{\w+}", english[key])), set(re.findall(r"{\w+}", text)))
        for key, text in german.items()
    }
    assert {k: v for k, v in placeholders.items() if v[0] != v[1]} == {}
    assert [k for k, v in german.items() if not v.strip()] == []


def _http_error_carrying_the_token(status: int) -> aiohttp.ClientResponseError:
    """An HTTP error as aiohttp raises it: its request info holds our headers."""
    request_info = RequestInfo(
        URL(API_URL),
        "POST",
        CIMultiDictProxy(CIMultiDict({"tokenId": TOKEN})),
        URL(API_URL),
    )
    return aiohttp.ClientResponseError(
        request_info, (), status=status, message="Service Unavailable"
    )


@pytest.mark.parametrize("status", [403, 503])
async def test_regression_http_error_does_not_leak_the_token(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    status: int,
) -> None:
    """Regression: an HTTP error put the token into the log and the UI.

    The error text was built with repr() of aiohttp's exception, which
    includes the request headers, among them tokenId.
    """
    respond(aioclient_mock, exc=_http_error_carrying_the_token(status))

    await async_setup(hass, config_entry)

    shown = json.dumps(
        [config_entry.reason, config_entry.error_reason_translation_placeholders]
    )
    assert TOKEN not in caplog.text
    assert TOKEN not in shown
    assert str(status) in shown


async def test_diagnostics_with_offline_notice_hide_the_serial(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The full download, including repair issues, holds no serial or token.

    Home Assistant adds the integration's repair issues to the download; for
    non-persistent issues like the offline notice it leaves out their
    placeholders (which contain the serial). This guards that.
    """
    respond(aioclient_mock, json=ok_response(utcDateTime="2026-10-03T13:23:04Z"))
    await async_setup(hass, config_entry)
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"dongle_offline_{config_entry.entry_id}"
    )

    assert await async_setup_component(hass, "diagnostics", {})
    client = await hass_client()
    response = await client.get(
        f"/api/diagnostics/config_entry/{config_entry.entry_id}"
    )
    payload = await response.text()

    assert response.status == 200
    assert '"issues"' in payload
    assert SERIAL not in payload
    assert TOKEN not in payload
