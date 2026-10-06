"""Fixtures for the Solax Cloud tests."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.core import HomeAssistant
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

from .common import API_ADDRESS, SERIAL, TOKEN, UNIQUE_ID, ok_response, respond


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load integrations from custom_components/."""


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """A config entry as the config flow creates it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=SERIAL,
        unique_id=UNIQUE_ID,
        data={CONF_API_ADDRESS: API_ADDRESS, CONF_TOKEN: TOKEN, CONF_SERIAL: SERIAL},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def solax_api(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """The cloud API answering with the live sample payload."""
    respond(aioclient_mock, json=ok_response())
    return aioclient_mock


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Keep entries created by a flow from being set up."""
    with patch(
        "custom_components.solax_cloud.async_setup_entry", return_value=True
    ) as mock:
        yield mock
