"""Coordinator for the Solax Cloud integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    SolaxCloudClient,
    SolaxCloudError,
    SolaxCloudSerialError,
    SolaxCloudTokenError,
)
from .const import DOMAIN, LOGGER

type SolaxCloudConfigEntry = ConfigEntry[SolaxCloudCoordinator]


class SolaxCloudCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls the realtime data of one dongle."""

    config_entry: SolaxCloudConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SolaxCloudConfigEntry,
        client: SolaxCloudClient,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=1),
        )
        self.client = client

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from solax API."""
        try:
            return await self.client.async_get_realtime_data()
        except SolaxCloudTokenError as err:
            raise ConfigEntryAuthFailed(
                f"Solax Cloud refused the token ID: {err}"
            ) from err
        except SolaxCloudSerialError as err:
            raise ConfigEntryAuthFailed(
                "The dongle serial number is not in the account of this token ID; "
                f"if the dongle was replaced, add it again: {err}"
            ) from err
        except SolaxCloudError as err:
            raise UpdateFailed(str(err)) from err
