"""Fixtures for the Solax Cloud tests."""

from __future__ import annotations

import pytest
import requests_mock as rm

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solax_cloud.const import CONF_SERIAL, CONF_TOKEN, DOMAIN

from .common import API_URL, SERIAL, TOKEN, UNIQUE_ID, ok_response


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
        data={CONF_TOKEN: TOKEN, CONF_SERIAL: SERIAL},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def solax_api(requests_mock: rm.Mocker) -> rm.Mocker:
    """The cloud API answering with the live sample payload."""
    requests_mock.get(API_URL, json=ok_response())
    return requests_mock
