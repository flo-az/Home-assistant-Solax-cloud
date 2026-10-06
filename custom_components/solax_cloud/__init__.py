"""The Solax Cloud integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SolaxCloudClient
from .const import (
    CONF_API_ADDRESS,
    CONF_SERIAL,
    CONF_TOKEN,
    DEFAULT_API_ADDRESS,
    DOMAIN,
)
from .coordinator import solaxcloudCoordinator
from .sensor import SENSOR_TYPES, sensor_unique_id

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Solax Cloud from a config entry."""

    client = SolaxCloudClient(
        async_get_clientsession(hass),
        entry.data.get(CONF_API_ADDRESS, DEFAULT_API_ADDRESS),
        entry.data[CONF_TOKEN],
        entry.data[CONF_SERIAL],
    )

    coordinator = solaxcloudCoordinator(hass, entry, client)

    # Raises ConfigEntryNotReady on a failed request, so Home Assistant
    # retries setup, or ConfigEntryAuthFailed, which asks for a new token.
    await coordinator.async_config_entry_first_refresh()

    _async_clean_up_entities(hass, entry)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


@callback
def _async_clean_up_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Bring registry entries from earlier versions in line with the sensors.

    The inverter serial used to read the misspelt field "inverterSn"; its
    entity keeps its id and history under the correct key. Sensors for fields
    this API never returns are removed.
    """
    assert entry.unique_id
    registry = er.async_get(hass)
    old_serial = sensor_unique_id(entry.unique_id, "inverterSn")
    new_serial = sensor_unique_id(entry.unique_id, "inverterSN")
    if (
        old_entity := registry.async_get_entity_id("sensor", DOMAIN, old_serial)
    ) and not registry.async_get_entity_id("sensor", DOMAIN, new_serial):
        registry.async_update_entity(old_entity, new_unique_id=new_serial)

    current = {sensor_unique_id(entry.unique_id, d.key) for d in SENSOR_TYPES}
    for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registered.unique_id not in current:
            registry.async_remove(registered.entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Solax config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
