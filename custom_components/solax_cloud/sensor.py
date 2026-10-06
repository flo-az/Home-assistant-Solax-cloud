"""Solax cloud."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    MAX_LENGTH_STATE_STATE,
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import solaxcloudCoordinator

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add Solax cloud entry."""
    coordinator: solaxcloudCoordinator = hass.data[DOMAIN][entry.entry_id]

    assert entry.unique_id
    unique_id = entry.unique_id

    async_add_entities(
        SolaxCloudSensor(unique_id, description, coordinator)
        for description in SENSOR_TYPES
    )


def sensor_unique_id(entry_unique_id: str, key: str) -> str:
    """Unique id of the sensor for an API field (kept from early versions)."""
    return f"{entry_unique_id}_test_{key}"


class SolaxCloudSensor(CoordinatorEntity[solaxcloudCoordinator], SensorEntity):
    """Representation of a Solax cloud sensor."""

    def __init__(
        self,
        unique_id: str,
        description: SensorEntityDescription,
        coordinator: solaxcloudCoordinator,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = sensor_unique_id(unique_id, description.key)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, unique_id)},
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> StateType | datetime:
       """Return the state of the sensor."""
       if self.entity_description.key in STATUS_CODES:
           # Undocumented codes give unknown rather than an invalid option.
           code = self.coordinator.data.get(self.entity_description.key)
           return STATUS_CODES[self.entity_description.key].get(str(code))
       if self.entity_description.key == "total_solar_power":
           # Calculate total solar production from all MPPT inputs
           data = self.coordinator.data
           readings = [r for key in PV_KEYS if (r := _number(data.get(key))) is not None]
           # No valid input at all is unknown, not 0 W.
           return _number(sum(readings)) if readings else None
       if self.entity_description.key == "utcDateTime":
           # v2 sends the upload instant in UTC, e.g. "2026-10-06T13:23:04Z".
           # (v1 sent the plant's wall time read as UTC+8 instead.)
           value = self.coordinator.data.get(self.entity_description.key)
           if not isinstance(value, str):
               return None
           try:
               parsed = datetime.fromisoformat(value)
               if parsed.tzinfo is None:
                   parsed = parsed.replace(tzinfo=dt_util.UTC)
               return parsed.astimezone(dt_util.UTC)
           except (ValueError, OverflowError):
               return None
       value = self.coordinator.data.get(self.entity_description.key)
       if self.entity_description.native_unit_of_measurement is not None:
           return _number(value)
       return _text(value)


# Far beyond any plant; larger values do not survive Home Assistant's
# 15-digit state formatting intact.
MAX_READING = 10**15


def _number(value: Any) -> int | float | None:
    """A reading as the API documents it (a plausible number), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # Also false for NaN and infinities.
    return value if abs(value) < MAX_READING else None


def _text(value: Any) -> str | None:
    """A text field that fits in a Home Assistant state, else None."""
    if isinstance(value, str) and len(value) <= MAX_LENGTH_STATE_STATE:
        return value
    return None


PV_KEYS = ["powerdc1", "powerdc2", "powerdc3", "powerdc4"]

# SolaXCloud User API V1.2, 8.1 "Device Status Mapping".
INVERTER_STATUS = {
    "100": "waiting",
    "101": "self_test",
    "102": "normal",
    "103": "recoverable_fault",
    "104": "permanent_fault",
    "105": "firmware_upgrade",
    "106": "eps_detection",
    "107": "off_grid",
    "108": "self_test_italy",
    "109": "sleep",
    "110": "standby",
    "111": "pv_wake_up_battery",
    "112": "generator_detection",
    "113": "generator",
    "114": "fast_shutdown_standby",
    "130": "vpp",
    "131": "tou_self_use",
    "132": "tou_charging",
    "133": "tou_discharging",
    "134": "tou_battery_off",
    "135": "tou_peak_shaving",
    "136": "generator_normal_operation",
    "137": "battery_expansion",
    "138": "on_grid_battery_heating",
    "139": "eps_battery_heating",
    "141": "normal_r1",
    "142": "normal_r2",
    "143": "normal_r3",
    "144": "normal_r4",
    "145": "normal_r5",
    "146": "normal_r6",
    "147": "normal_r7",
    "148": "normal_ss",
    "150": "self_use",
    "151": "force_time_use",
    "152": "back_up",
    "153": "feedin_priority",
    "154": "demand",
    "155": "constant_power",
    "160": "openadr",
}
# SolaXCloud User API V1.2, 7.1, batStatus.
BATTERY_STATUS = {"0": "normal", "1": "fault", "2": "disconnected"}
STATUS_CODES = {"inverterStatus": INVERTER_STATUS, "batStatus": BATTERY_STATUS}


def _power(key: str, name: str, translation_key: str, **kwargs) -> SensorEntityDescription:
    return SensorEntityDescription(
        key=key,
        name=name,
        translation_key=translation_key,
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        **kwargs,
    )


def _energy(key: str, name: str, translation_key: str) -> SensorEntityDescription:
    return SensorEntityDescription(
        key=key,
        name=name,
        translation_key=translation_key,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
    )


# One sensor per field of the realtime response (SolaXCloud User API V1.2,
# 7.1), except inverterType, plus the computed PV total. Names are kept from
# earlier versions.
SENSOR_TYPES = [
    SensorEntityDescription(
        key="inverterSN",
        name="Inverter serial",
        translation_key="inverter_serial",
    ),
    SensorEntityDescription(
        key="sn",
        name="Pocket serial",
        translation_key="pocket_serial",
    ),
    SensorEntityDescription(
        key="inverterStatus",
        name="Inverter status",
        translation_key="inverter_status",
        device_class=SensorDeviceClass.ENUM,
        options=list(INVERTER_STATUS.values()),
    ),
    _power("acpower", "AC Power", "ac_power"),
    _energy("yieldtoday", "Yield today", "yield_today"),
    _energy("yieldtotal", "Yield total", "yield_total"),
    _power("feedinpower", "Feedin Power", "feedin_power"),
    _energy("feedinenergy", "Feedin energy", "feedin_energy"),
    _energy("consumeenergy", "Consume energy", "consume_energy"),
    _power(
        "feedinpowerM2",
        "Meter 2 power",
        "meter2_power",
        entity_registry_enabled_default=False,
    ),
    _power("powerdc1", "MPPT1 power", "mppt1_power"),
    _power("powerdc2", "MPPT2 power", "mppt2_power"),
    _power("powerdc3", "MPPT3 power", "mppt3_power", entity_registry_enabled_default=False),
    _power("powerdc4", "MPPT4 power", "mppt4_power", entity_registry_enabled_default=False),
    _power("total_solar_power", "Total Solar Power", "total_solar_power"),
    _power("batPower", "Battery power", "battery_power"),
    SensorEntityDescription(
        key="soc",
        name="State of charge",
        translation_key="soc",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="batStatus",
        name="Battery status",
        translation_key="battery_status",
        device_class=SensorDeviceClass.ENUM,
        options=list(BATTERY_STATUS.values()),
    ),
    _power("peps1", "EPS phase 1 power", "eps_phase1_power"),
    _power("peps2", "EPS phase 2 power", "eps_phase2_power"),
    _power("peps3", "EPS phase 3 power", "eps_phase3_power"),
    SensorEntityDescription(
        key="uploadTime",
        name="Last cloud upload",
        translation_key="upload_time",
    ),
    SensorEntityDescription(
        key="utcDateTime",
        name="UTC Date Time",
        translation_key="utc_date_time",
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
]
