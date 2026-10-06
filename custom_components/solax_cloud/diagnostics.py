"""Diagnostics for the Solax Cloud integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_SERIAL, CONF_TOKEN
from .coordinator import SolaxCloudConfigEntry

TO_REDACT = {CONF_TOKEN, CONF_SERIAL, "inverterSN", "sn"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SolaxCloudConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry, without token or serials."""
    coordinator = entry.runtime_data
    interval = coordinator.update_interval
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "polling": {
            "last_update_success": coordinator.last_update_success,
            "update_interval_seconds": interval.total_seconds() if interval else None,
            "stale": coordinator.stale,
        },
        "data": async_redact_data(coordinator.data, TO_REDACT),
    }
