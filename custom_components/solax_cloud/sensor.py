"""Sensors for the Solax Cloud integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Self

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorExtraStoredData,
    SensorStateClass,
)
from homeassistant.const import (
    MAX_LENGTH_STATE_STATE,
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import SolaxCloudConfigEntry, SolaxCloudCoordinator

PV_KEYS = ["powerdc1", "powerdc2", "powerdc3", "powerdc4"]

# Far beyond any plant; larger values do not survive Home Assistant's
# 15-digit state formatting intact.
MAX_READING = 10**15

# Uploads further apart than this are not bridged when adding up energy.
MAX_UPLOAD_GAP = timedelta(minutes=15)

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


def sensor_unique_id(entry_unique_id: str, key: str) -> str:
    """Unique id of the sensor for an API field (kept from early versions)."""
    return f"{entry_unique_id}_test_{key}"


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


def _upload_instant(data: dict[str, Any]) -> datetime | None:
    """When the data was uploaded; v2 sends utcDateTime in real UTC.

    (v1 sent the plant's wall time read as UTC+8 instead.)
    """
    value = data.get("utcDateTime")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_util.UTC)
        return parsed.astimezone(dt_util.UTC)
    except (ValueError, OverflowError):
        return None


def _pv_total(data: dict[str, Any]) -> int | float | None:
    """Sum of all MPPT inputs; no valid input at all is unknown, not 0 W."""
    readings = [r for key in PV_KEYS if (r := _number(data.get(key))) is not None]
    return _number(sum(readings)) if readings else None


@dataclass(frozen=True, kw_only=True)
class SolaxSensorEntityDescription(SensorEntityDescription):
    """A sensor and how to read its value from the realtime result."""

    value_fn: Callable[[dict[str, Any]], StateType | datetime] | None = None

    def value(self, data: dict[str, Any]) -> StateType | datetime:
        """The sensor's value for a realtime result."""
        if self.value_fn is not None:
            return self.value_fn(data)
        if self.native_unit_of_measurement is not None:
            return _number(data.get(self.key))
        return _text(data.get(self.key))


def _status(
    key: str, translation_key: str, codes: dict[str, str]
) -> SolaxSensorEntityDescription:
    return SolaxSensorEntityDescription(
        key=key,
        translation_key=translation_key,
        device_class=SensorDeviceClass.ENUM,
        options=list(codes.values()),
        # Undocumented codes give unknown rather than an invalid option.
        value_fn=lambda data: codes.get(str(data.get(key))),
    )


def _power(
    key: str, translation_key: str, enabled: bool = True
) -> SolaxSensorEntityDescription:
    return SolaxSensorEntityDescription(
        key=key,
        translation_key=translation_key,
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=enabled,
    )


def _energy(key: str, translation_key: str) -> SolaxSensorEntityDescription:
    return SolaxSensorEntityDescription(
        key=key,
        translation_key=translation_key,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
    )


# One sensor per field of the realtime response (SolaXCloud User API V1.2,
# 7.1), except inverterType, plus the computed PV total.
SENSOR_TYPES: list[SolaxSensorEntityDescription] = [
    SolaxSensorEntityDescription(key="inverterSN", translation_key="inverter_serial"),
    SolaxSensorEntityDescription(key="sn", translation_key="pocket_serial"),
    _status("inverterStatus", "inverter_status", INVERTER_STATUS),
    _power("acpower", "ac_power"),
    _energy("yieldtoday", "yield_today"),
    _energy("yieldtotal", "yield_total"),
    _power("feedinpower", "feedin_power"),
    _energy("feedinenergy", "feedin_energy"),
    _energy("consumeenergy", "consume_energy"),
    _power("feedinpowerM2", "meter2_power", enabled=False),
    _power("powerdc1", "mppt1_power"),
    _power("powerdc2", "mppt2_power"),
    _power("powerdc3", "mppt3_power", enabled=False),
    _power("powerdc4", "mppt4_power", enabled=False),
    SolaxSensorEntityDescription(
        key="total_solar_power",
        translation_key="total_solar_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_pv_total,
    ),
    _power("batPower", "battery_power"),
    SolaxSensorEntityDescription(
        key="soc",
        translation_key="soc",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    _status("batStatus", "battery_status", BATTERY_STATUS),
    _power("peps1", "eps_phase1_power"),
    _power("peps2", "eps_phase2_power"),
    _power("peps3", "eps_phase3_power"),
    SolaxSensorEntityDescription(key="uploadTime", translation_key="upload_time"),
    SolaxSensorEntityDescription(
        key="utcDateTime",
        translation_key="utc_date_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_upload_instant,
    ),
]


@dataclass(frozen=True, kw_only=True)
class BatteryEnergyEntityDescription(SensorEntityDescription):
    """Energy into (+1) or out of (-1) the battery, added up from batPower."""

    direction: int


BATTERY_ENERGY_TYPES = [
    BatteryEnergyEntityDescription(
        key="battery_charge_energy",
        translation_key="battery_charge_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        direction=1,
    ),
    BatteryEnergyEntityDescription(
        key="battery_discharge_energy",
        translation_key="battery_discharge_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        direction=-1,
    ),
]

ALL_KEYS = [d.key for d in (*SENSOR_TYPES, *BATTERY_ENERGY_TYPES)]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolaxCloudConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add Solax cloud entry."""
    coordinator = entry.runtime_data
    assert entry.unique_id

    async_add_entities(
        [
            *(
                SolaxCloudSensor(entry.unique_id, description, coordinator)
                for description in SENSOR_TYPES
            ),
            *(
                BatteryEnergySensor(entry.unique_id, description, coordinator)
                for description in BATTERY_ENERGY_TYPES
            ),
        ]
    )


class SolaxCloudEntity(CoordinatorEntity[SolaxCloudCoordinator]):
    """An entity of the dongle's device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        unique_id: str,
        description: SensorEntityDescription,
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


class SolaxCloudSensor(SolaxCloudEntity, SensorEntity):
    """A value of the realtime result."""

    entity_description: SolaxSensorEntityDescription

    @property
    def native_value(self) -> StateType | datetime:
        """Return the state of the sensor."""
        return self.entity_description.value(self.coordinator.data)


@dataclass
class BatteryEnergyExtraStoredData(SensorExtraStoredData):
    """The total plus the last upload it was added up to."""

    last_upload: datetime | None
    last_power: float | None

    def as_dict(self) -> dict[str, Any]:
        """Return a dict representation of the stored data."""
        return {
            **super().as_dict(),
            "last_upload": self.last_upload.isoformat() if self.last_upload else None,
            "last_power": self.last_power,
        }

    @classmethod
    def from_dict(cls, restored: dict[str, Any]) -> Self | None:
        """Initialize the stored data from a dict; series parts are optional."""
        if (sensor_data := SensorExtraStoredData.from_dict(restored)) is None:
            return None
        last_upload: datetime | None = None
        last_power = _number(restored.get("last_power"))
        if isinstance(raw := restored.get("last_upload"), str):
            try:
                last_upload = datetime.fromisoformat(raw)
            except ValueError:
                last_upload = None
        if last_upload is None or last_upload.tzinfo is None or last_power is None:
            last_upload, last_power = None, None
        return cls(
            sensor_data.native_value,
            sensor_data.native_unit_of_measurement,
            last_upload,
            None if last_power is None else float(last_power),
        )


class BatteryEnergySensor(SolaxCloudEntity, RestoreSensor):
    """Energy charged into or discharged from the battery.

    batPower (positive while charging) is a snapshot per upload. Each pair
    of consecutive uploads adds the average of the two readings, each
    limited to this sensor's direction, over the time between them.
    Repeated polls of one upload, uploads out of order, gaps longer than
    MAX_UPLOAD_GAP and intervals with a missing reading add nothing.
    """

    entity_description: BatteryEnergyEntityDescription

    def __init__(
        self,
        unique_id: str,
        description: BatteryEnergyEntityDescription,
        coordinator: SolaxCloudCoordinator,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(unique_id, description, coordinator)
        self._total = 0.0
        self._last_upload: datetime | None = None
        self._last_power: float | None = None

    @property
    def native_value(self) -> float:
        """Return the total in kWh."""
        return self._total

    @property
    def extra_restore_state_data(self) -> BatteryEnergyExtraStoredData:
        """Return what is needed to continue the series after a restart."""
        return BatteryEnergyExtraStoredData(
            self._total,
            UnitOfEnergy.KILO_WATT_HOUR,
            self._last_upload,
            self._last_power,
        )

    async def async_added_to_hass(self) -> None:
        """Continue from the stored total and series."""
        await super().async_added_to_hass()
        if (restored := await self.async_get_last_extra_data()) is not None and (
            stored := BatteryEnergyExtraStoredData.from_dict(restored.as_dict())
        ) is not None:
            if (total := _number(stored.native_value)) is not None and total >= 0:
                self._total = float(total)
            self._last_upload = stored.last_upload
            self._last_power = stored.last_power
        self._add_upload(self.coordinator.data)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Add the newest upload, then write the state."""
        self._add_upload(self.coordinator.data)
        super()._handle_coordinator_update()

    def _add_upload(self, data: dict[str, Any] | None) -> None:
        if not data or (upload := _upload_instant(data)) is None:
            return
        if self._last_upload is not None and upload <= self._last_upload:
            return
        power = _number(data.get("batPower"))
        if (
            power is not None
            and self._last_power is not None
            and self._last_upload is not None
            and upload - self._last_upload <= MAX_UPLOAD_GAP
        ):
            direction = self.entity_description.direction
            average_w = (
                max(direction * self._last_power, 0) + max(direction * power, 0)
            ) / 2
            hours = (upload - self._last_upload).total_seconds() / 3600
            self._total += average_w * hours / 1000
        self._last_upload = upload
        self._last_power = None if power is None else float(power)
