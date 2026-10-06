"""The Solax Cloud integration."""

from __future__ import annotations

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
from .coordinator import SolaxCloudConfigEntry, SolaxCloudCoordinator
from .entity import sensor_unique_id
from .sensor import ALL_KEYS

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: SolaxCloudConfigEntry) -> bool:
    """Set up Solax Cloud from a config entry."""
    client = SolaxCloudClient(
        async_get_clientsession(hass),
        entry.data.get(CONF_API_ADDRESS, DEFAULT_API_ADDRESS),
        entry.data[CONF_TOKEN],
        entry.data[CONF_SERIAL],
    )
    coordinator = SolaxCloudCoordinator(hass, entry, client)

    # Raises ConfigEntryNotReady on a failed request, so Home Assistant
    # retries setup, or ConfigEntryAuthFailed, which asks for a new token.
    await coordinator.async_config_entry_first_refresh()

    _async_clean_up_entities(hass, entry)

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


@callback
def _async_clean_up_entities(hass: HomeAssistant, entry: SolaxCloudConfigEntry) -> None:
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

    current = {sensor_unique_id(entry.unique_id, key) for key in ALL_KEYS}
    for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registered.unique_id not in current:
            registry.async_remove(registered.entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: SolaxCloudConfigEntry) -> bool:
    """Unload Solax config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        entry.runtime_data.async_clear_offline_issue()
    return unload_ok
