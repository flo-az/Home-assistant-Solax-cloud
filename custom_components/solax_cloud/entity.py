"""Base entity for the Solax Cloud integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SolaxCloudCoordinator


def sensor_unique_id(entry_unique_id: str, key: str) -> str:
    """Unique id of the sensor for an API field (kept from early versions)."""
    return f"{entry_unique_id}_test_{key}"


class SolaxCloudEntity(CoordinatorEntity[SolaxCloudCoordinator]):
    """An entity of the dongle's device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        unique_id: str,
        description: EntityDescription,
        coordinator: SolaxCloudCoordinator,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = sensor_unique_id(unique_id, description.key)
        serial = coordinator.client.serial
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, unique_id)},
            name=serial,
            manufacturer="SolaX Power",
            serial_number=serial,
        )
