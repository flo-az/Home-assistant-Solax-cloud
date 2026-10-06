"""Tests for which entities the integration registers."""

from __future__ import annotations

import requests_mock as rm

from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solax_cloud.const import DOMAIN

from .common import UNIQUE_ID, async_setup, entity_id, state

PREFIX = f"{UNIQUE_ID}_test_"

# Every field of the documented realtime response (SolaXCloud User API V1.2,
# 7.1) except inverterType, plus the computed PV total.
DOCUMENTED_KEYS = {
    "inverterSN",
    "sn",
    "acpower",
    "yieldtoday",
    "yieldtotal",
    "feedinpower",
    "feedinenergy",
    "consumeenergy",
    "feedinpowerM2",
    "soc",
    "peps1",
    "peps2",
    "peps3",
    "inverterStatus",
    "uploadTime",
    "batPower",
    "powerdc1",
    "powerdc2",
    "powerdc3",
    "powerdc4",
    "batStatus",
    "utcDateTime",
    "total_solar_power",
}


def _registered_keys(hass: HomeAssistant, entry: MockConfigEntry) -> set[str]:
    entries = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    return {e.unique_id.removeprefix(PREFIX) for e in entries}


async def test_registers_one_sensor_per_documented_field(
    hass: HomeAssistant, config_entry: MockConfigEntry, solax_api: rm.Mocker
) -> None:
    """Only fields the API returns get a sensor; none is unknown by design."""
    await async_setup(hass, config_entry)

    assert _registered_keys(hass, config_entry) == DOCUMENTED_KEYS


async def test_unused_inputs_are_disabled_by_default(
    hass: HomeAssistant, config_entry: MockConfigEntry, solax_api: rm.Mocker
) -> None:
    """Inputs most inverters lack (MPPT 3/4, a second meter) start disabled."""
    await async_setup(hass, config_entry)

    registry = er.async_get(hass)
    disabled = {
        key
        for key in DOCUMENTED_KEYS
        if registry.async_get(entity_id(hass, key)).disabled_by
        is er.RegistryEntryDisabler.INTEGRATION
    }
    assert disabled == {"powerdc3", "powerdc4", "feedinpowerM2"}


async def test_regression_inverter_serial_reads_documented_field(
    hass: HomeAssistant, config_entry: MockConfigEntry, solax_api: rm.Mocker
) -> None:
    """Regression: the inverter serial read "inverterSn" and was always unknown.

    The API field is "inverterSN". The existing entity must keep its entity id
    (and history) when its unique id moves to the correct key.
    """
    registry = er.async_get(hass)
    registry.async_get_or_create(
        SENSOR_DOMAIN,
        DOMAIN,
        f"{PREFIX}inverterSn",
        config_entry=config_entry,
        suggested_object_id="inverter_serial",
    )

    await async_setup(hass, config_entry)

    assert entity_id(hass, "inverterSN") == "sensor.inverter_serial"
    assert state(hass, "inverterSN") == "H3TEST0000001"


async def test_regression_sensors_for_missing_fields_are_removed(
    hass: HomeAssistant, config_entry: MockConfigEntry, solax_api: rm.Mocker
) -> None:
    """Regression: 36 sensors read fields this API never returns.

    Registry entries left from earlier versions are removed on setup so they
    stop showing up as unavailable entities.
    """
    registry = er.async_get(hass)
    for key in ("vdc1", "ratedPower", "pvenergy", "batcycle"):
        registry.async_get_or_create(
            SENSOR_DOMAIN, DOMAIN, f"{PREFIX}{key}", config_entry=config_entry
        )

    await async_setup(hass, config_entry)

    assert _registered_keys(hass, config_entry) == DOCUMENTED_KEYS
