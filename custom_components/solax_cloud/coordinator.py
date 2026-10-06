"""Coordinator for solaxcloud."""

from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SolaxCloudAuthError, SolaxCloudClient, SolaxCloudError
from .const import DOMAIN, LOGGER


class solaxcloudCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching solax cloud data."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: SolaxCloudClient
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
        except SolaxCloudAuthError as err:
            raise ConfigEntryAuthFailed(
                f"Solax Cloud refused the token ID or serial number: {err}"
            ) from err
        except SolaxCloudError as err:
            raise UpdateFailed(str(err)) from err
