"""Coordinator for solaxcloud."""

from datetime import timedelta
from typing import Any

from solaxcloud.solaxcloud import solaxcloud

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, LOGGER


class solaxcloudCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching solax cloud data."""

    def __init__(self, hass: HomeAssistant, api: solaxcloud) -> None:
        """Initialize."""
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=1),
        )
        self.api = api

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from solax API."""

        dictionary = await self.hass.async_add_executor_job(self.api.get_realtime_data)
        if dictionary is None:
            # solaxcloud returns None whenever the API answers success=false.
            raise UpdateFailed(
                "Solax Cloud rejected the request; check the token ID and serial number"
            )

        return dictionary
