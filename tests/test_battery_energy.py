"""Tests for the battery charge/discharge energy sensors.

The API reports battery power (positive while charging) once per upload; the
energy dashboard needs kWh totals. Expected values are hand-computed:
3000 W for 5 minutes is 3000 * 5 / 60 / 1000 = 0.25 kWh.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.solax_cloud.const import DOMAIN

from .common import (
    OPERATION_FAILED,
    UNIQUE_ID,
    async_poll,
    async_setup,
    ok_response,
    respond,
    state,
)

T0 = datetime(2026, 10, 6, 13, 0, 0, tzinfo=UTC)
CHARGE = "battery_charge_energy"
DISCHARGE = "battery_discharge_energy"


def upload(
    minute: float, power: float | None, pv: float | None = 0.0
) -> dict[str, Any]:
    """A response for an upload `minute` minutes after T0.

    `power` is the battery power, `pv` the total PV power (all on MPPT1).
    """
    at = T0 + timedelta(minutes=minute)
    return {
        "json": ok_response(
            batPower=power,
            powerdc1=pv,
            powerdc2=None,
            utcDateTime=at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            uploadTime=at.strftime("%Y-%m-%d %H:%M:%S"),
        )
    }


def totals(hass: HomeAssistant) -> tuple[float, float]:
    """Current (charge, discharge) totals in kWh."""
    return float(state(hass, CHARGE)), float(state(hass, DISCHARGE))


async def run(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    api: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    responses: list[dict[str, Any]],
) -> None:
    """Set up with the first response, then poll once per further response."""
    freezer.move_to(T0)
    respond(api, **responses[0])
    await async_setup(hass, entry)
    for response in responses[1:]:
        respond(api, **response)
        await async_poll(hass, freezer)


async def test_energy_sensors_fit_the_energy_dashboard(
    hass: HomeAssistant, config_entry: MockConfigEntry, solax_api: AiohttpClientMocker
) -> None:
    """The dashboard accepts kWh energy sensors that only count up."""
    await async_setup(hass, config_entry)

    for key in (CHARGE, DISCHARGE):
        registry = er.async_get(hass)
        entity = registry.async_get_entity_id(
            SENSOR_DOMAIN, DOMAIN, f"{UNIQUE_ID}_test_{key}"
        )
        attributes = hass.states.get(entity).attributes
        assert (
            attributes["device_class"],
            attributes["unit_of_measurement"],
            attributes["state_class"],
        ) == ("energy", "kWh", "total_increasing")


async def test_charging_adds_up_between_uploads(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """3000 W over two 5-minute intervals is 0.5 kWh charged."""
    await run(
        hass,
        config_entry,
        aioclient_mock,
        freezer,
        [upload(0, 3000), upload(5, 3000), upload(10, 3000)],
    )

    assert totals(hass) == pytest.approx((0.5, 0.0))


async def test_discharging_is_counted_separately(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Negative power is discharge: 1200 W for 5 minutes is 0.1 kWh."""
    await run(
        hass,
        config_entry,
        aioclient_mock,
        freezer,
        [upload(0, -1200), upload(5, -1200)],
    )

    assert totals(hass) == pytest.approx((0.0, 0.1))


async def test_regression_sign_change_is_split_where_power_crosses_zero(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Regression: a sign change counted energy on both sides twice over.

    Averaging each side's clamped readings gave 0.125 kWh charged and
    0.0417 kWh discharged for 3000 W -> -1000 W over 5 minutes. The line
    between the readings crosses zero after 3.75 minutes: 3000 W * 3.75 min / 2
    = 0.09375 kWh charged, 1000 W * 1.25 min / 2 = 0.0104 kWh discharged.
    """
    await run(
        hass, config_entry, aioclient_mock, freezer, [upload(0, 3000), upload(5, -1000)]
    )

    assert totals(hass) == pytest.approx(
        (3000 * 3.75 / 2 / 60 / 1000, 1000 * 1.25 / 2 / 60 / 1000)
    )


async def test_repeated_polls_of_one_upload_count_once(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Polling every minute sees each 5-minute upload several times."""
    await run(
        hass,
        config_entry,
        aioclient_mock,
        freezer,
        [upload(0, 3000), upload(0, 3000), upload(5, 3000), upload(5, 3000)],
    )

    assert totals(hass) == pytest.approx((0.25, 0.0))


async def test_long_gap_is_not_guessed(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """More than 15 minutes between uploads is skipped, then counting resumes."""
    await run(
        hass,
        config_entry,
        aioclient_mock,
        freezer,
        [upload(0, 3000), upload(30, 3000), upload(35, 3000)],
    )

    assert totals(hass) == pytest.approx((0.25, 0.0))


async def test_missing_power_breaks_the_series(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An interval with an unknown end is skipped, not treated as 0 W."""
    await run(
        hass,
        config_entry,
        aioclient_mock,
        freezer,
        [upload(0, 3000), upload(5, None), upload(10, 3000), upload(15, 3000)],
    )

    assert totals(hass) == pytest.approx((0.25, 0.0))


async def test_upload_from_the_past_is_ignored(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An older upload (e.g. a stale cloud replica) never subtracts energy."""
    await run(
        hass,
        config_entry,
        aioclient_mock,
        freezer,
        [upload(0, 3000), upload(5, 3000), upload(2, 3000), upload(10, 3000)],
    )

    assert totals(hass) == pytest.approx((0.5, 0.0))


async def test_failed_poll_keeps_totals_and_series(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A failed poll shows unavailable but loses neither total nor baseline."""
    await run(hass, config_entry, aioclient_mock, freezer, [upload(0, 3000)])
    respond(aioclient_mock, json=OPERATION_FAILED)
    await async_poll(hass, freezer)
    assert state(hass, CHARGE) == STATE_UNAVAILABLE

    respond(aioclient_mock, **upload(5, 3000))
    await async_poll(hass, freezer)

    assert totals(hass) == pytest.approx((0.25, 0.0))


async def _preregister(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, str]:
    registry = er.async_get(hass)
    return {
        key: registry.async_get_or_create(
            SENSOR_DOMAIN,
            DOMAIN,
            f"{UNIQUE_ID}_test_{key}",
            config_entry=entry,
            suggested_object_id=key,
        ).entity_id
        for key in (CHARGE, DISCHARGE)
    }


async def test_totals_and_series_survive_restart(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """After a restart counting continues from the stored total and upload."""
    ids = await _preregister(hass, config_entry)
    stored_series = {
        "last_upload": T0.isoformat(),
        "last_power": 3000.0,
    }
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(ids[CHARGE], "1.5"),
                {
                    "native_value": 1.5,
                    "native_unit_of_measurement": "kWh",
                    **stored_series,
                },
            ),
            (
                State(ids[DISCHARGE], "0.2"),
                {
                    "native_value": 0.2,
                    "native_unit_of_measurement": "kWh",
                    **stored_series,
                },
            ),
        ],
    )

    await run(hass, config_entry, aioclient_mock, freezer, [upload(5, 3000)])

    assert totals(hass) == pytest.approx((1.75, 0.2))


async def test_restored_total_without_series_starts_a_new_one(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A stored total without its last upload keeps the total, adds nothing yet."""
    ids = await _preregister(hass, config_entry)
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(ids[CHARGE], "1.5"),
                {"native_value": 1.5, "native_unit_of_measurement": "kWh"},
            ),
            (
                State(ids[DISCHARGE], "0.2"),
                {"native_value": 0.2, "native_unit_of_measurement": "kWh"},
            ),
        ],
    )

    await run(
        hass, config_entry, aioclient_mock, freezer, [upload(5, 3000), upload(10, 3000)]
    )

    assert totals(hass) == pytest.approx((1.75, 0.2))


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        # No usable sensor data at all: start from zero.
        ({"last_upload": T0.isoformat(), "last_power": 3000.0}, (0.25, 1.75)),
        # Unreadable upload time: keep the total, start a new series.
        (
            {
                "native_value": 1.5,
                "native_unit_of_measurement": "kWh",
                "last_upload": "yesterday",
                "last_power": 3000.0,
            },
            (1.75, 1.75),
        ),
        # Upload time without a zone: same.
        (
            {
                "native_value": 1.5,
                "native_unit_of_measurement": "kWh",
                "last_upload": "2026-10-06T13:00:00",
                "last_power": 3000.0,
            },
            (1.75, 1.75),
        ),
        # A negative total can only be corruption: start from zero.
        (
            {
                "native_value": -4.0,
                "native_unit_of_measurement": "kWh",
                "last_upload": T0.isoformat(),
                "last_power": 3000.0,
            },
            (0.5, 1.75),
        ),
    ],
    ids=["no-total", "unreadable-upload", "naive-upload", "negative-total"],
)
async def test_corrupt_stored_data_is_handled(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    stored: dict[str, Any],
    expected: tuple[float, float],
) -> None:
    """Stored state that cannot be trusted never breaks or skews the totals.

    Uploads at minutes 5 and 10, charging 3000 W: 0.25 kWh from the new
    series, 0.5 kWh when the stored series (minute 0) is usable. The charge
    sensor gets `stored`, the discharge sensor a clean 1.75 kWh total with
    the same series, so both columns show the expected outcome.
    """
    ids = await _preregister(hass, config_entry)
    mock_restore_cache_with_extra_data(
        hass,
        [
            (State(ids[CHARGE], "x"), stored),
            (
                State(ids[DISCHARGE], "1.75"),
                {"native_value": 1.75, "native_unit_of_measurement": "kWh"},
            ),
        ],
    )

    await run(
        hass, config_entry, aioclient_mock, freezer, [upload(5, 3000), upload(10, 3000)]
    )

    assert totals(hass) == pytest.approx(expected)


async def test_regression_future_upload_does_not_freeze_battery_energy(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Regression: one upload stamped in the future stopped all counting.

    It became the last upload, every real upload compared as older and was
    ignored, and the stored series kept it that way across restarts.
    """
    await run(
        hass,
        config_entry,
        aioclient_mock,
        freezer,
        [
            upload(0, 3000),
            upload(60 * 24 * 365 * 73, 3000),
            upload(5, 3000),
            upload(10, 3000),
        ],
    )

    assert totals(hass) == pytest.approx((0.5, 0.0))


async def test_totals_continue_across_reload(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """What the sensors store when unloaded is what they continue from."""
    await run(
        hass, config_entry, aioclient_mock, freezer, [upload(0, 3000), upload(5, 3000)]
    )

    respond(aioclient_mock, **upload(10, 3000))
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert totals(hass) == pytest.approx((0.5, 0.0))


@pytest.mark.parametrize(
    "series",
    [
        {"last_upload": T0.isoformat(), "last_power": "lots"},
        {"last_upload": (T0 + timedelta(days=365)).isoformat(), "last_power": 3000.0},
    ],
    ids=["invalid-power", "upload-in-the-future"],
)
async def test_untrustworthy_stored_series_starts_a_new_one(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    series: dict[str, Any],
) -> None:
    """The total is kept, but counting restarts from the next upload."""
    ids = await _preregister(hass, config_entry)
    stored = {"native_value": 1.5, "native_unit_of_measurement": "kWh", **series}
    mock_restore_cache_with_extra_data(
        hass,
        [(State(ids[CHARGE], "1.5"), stored), (State(ids[DISCHARGE], "1.5"), stored)],
    )

    await run(
        hass, config_entry, aioclient_mock, freezer, [upload(5, 3000), upload(10, 3000)]
    )

    assert totals(hass) == pytest.approx((1.75, 1.5))


async def test_solar_energy_adds_up_pv_power(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Solar energy counts PV input, not the inverter's AC output.

    The API's yield counters are AC output: they include battery discharge
    and miss PV that went into the battery. 1000 W -> 3000 W over 5 min is
    2000 W * 5 min = 0.1667 kWh, whatever the battery does meanwhile.
    """
    await run(
        hass,
        config_entry,
        aioclient_mock,
        freezer,
        [upload(0, 2500, pv=1000), upload(5, -500, pv=3000)],
    )

    assert float(state(hass, "solar_energy")) == pytest.approx(2000 * 5 / 60 / 1000)
    attributes = hass.states.get(
        er.async_get(hass).async_get_entity_id(
            SENSOR_DOMAIN, DOMAIN, f"{UNIQUE_ID}_test_solar_energy"
        )
    ).attributes
    assert (attributes["device_class"], attributes["state_class"]) == (
        "energy",
        "total_increasing",
    )


async def test_regression_cloud_clock_correction_does_not_stop_energy(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Regression: one stamp 12 h ahead stopped the energy count for 12 h.

    Every real upload after it compared as older. Once the tracker adopts the
    corrected clock (three advancing older stamps), counting resumes: only
    the interval 15 -> 20 min counts, 3000 W for 5 min = 0.25 kWh.
    """
    await run(
        hass,
        config_entry,
        aioclient_mock,
        freezer,
        [
            upload(0, 3000),
            upload(12 * 60, 3000),
            upload(5, 3000),
            upload(10, 3000),
            upload(15, 3000),
            upload(20, 3000),
        ],
    )

    assert totals(hass) == pytest.approx((0.25, 0.0))
