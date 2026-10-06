"""The Solax Cloud integration."""

from __future__ import annotations

from solaxcloud.solaxcloud import solaxcloud

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_SERIAL, CONF_TOKEN, DOMAIN
from .coordinator import solaxcloudCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Solax Cloud from a config entry."""

    api = solaxcloud(
        token=entry.data[CONF_TOKEN],
        registration_number=entry.data[CONF_SERIAL],
    )

    coordinator = solaxcloudCoordinator(hass, api)

    # Raises ConfigEntryNotReady on any failed request, so Home Assistant
    # retries setup instead of giving up.
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Solax config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
