"""Property-based tests: the integration against arbitrary API behaviour."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import itertools
import math
import sys
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from freezegun.api import FrozenDateTimeFactory
from hypothesis import HealthCheck, example, given, settings, strategies as st
import pytest

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.solax_cloud.const import (
    CONF_API_ADDRESS,
    CONF_SERIAL,
    CONF_TOKEN,
    DOMAIN,
)

from .common import (
    API_ADDRESS,
    OPERATION_FAILED,
    SERIAL,
    SERIAL_REJECTED,
    TOKEN,
    TOKEN_REJECTED,
    async_poll,
    async_setup,
    entity_id,
    ok_response,
    respond,
    state,
)

# One Home Assistant instance is shared by all examples of a test; each
# example only feeds it new API responses.
SHARED_HASS = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

# Documented float fields that have an enabled sensor (User API V1.2, 7.1).
NUMERIC_KEYS = [
    "acpower",
    "yieldtoday",
    "yieldtotal",
    "feedinpower",
    "feedinenergy",
    "consumeenergy",
    "soc",
    "peps1",
    "peps2",
    "peps3",
    "batPower",
    "powerdc1",
    "powerdc2",
]
PV_KEYS = ["powerdc1", "powerdc2", "powerdc3", "powerdc4"]
STATUS_KEYS = ["inverterStatus", "batStatus"]
TEXT_KEYS = ["inverterSN", "sn", "uploadTime"]
SENSOR_KEYS = [
    *NUMERIC_KEYS,
    *STATUS_KEYS,
    *TEXT_KEYS,
    "total_solar_power",
    "utcDateTime",
]
# Documented codes and their states (User API V1.2, 7.1 and 8.1).
KNOWN_STATUS = {
    "inverterStatus": {"100": "waiting", "102": "normal", "109": "sleep"},
    "batStatus": {"0": "normal", "1": "fault", "2": "disconnected"},
}
# DataUpdateCoordinator's one-line report of a failed request.
COORDINATOR_FAILURE_REPORTS = (
    "Error requesting solax_cloud data",
    "Error fetching solax_cloud data",
    "Timeout fetching solax_cloud data",
    "Authentication failed while fetching solax_cloud data",
)

readings = st.one_of(
    st.none(),
    st.integers(min_value=-(10**7), max_value=10**8),
    st.floats(min_value=-1e7, max_value=1e8, allow_nan=False),
)
status_codes = st.one_of(
    st.none(),
    st.sampled_from(["0", "1", "2", "100", "102", "109"]),
    st.text(max_size=6),
)
any_datetime = st.datetimes()
timestamps = st.one_of(
    st.none(),
    any_datetime.map(lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")),
    any_datetime.map(lambda d: d.isoformat()),
    st.text(max_size=40),
)
json_values = st.recursive(
    st.none()
    | st.booleans()
    # The mock encodes bodies with orjson, which stops at 64-bit integers.
    | st.integers(min_value=-(2**63), max_value=2**63 - 1)
    | st.floats(allow_nan=False)
    | st.text(max_size=300),
    lambda children: (
        st.lists(children, max_size=3)
        | st.dictionaries(st.text(max_size=8), children, max_size=3)
    ),
    max_leaves=8,
)


@st.composite
def results(draw: st.DrawFn) -> dict[str, Any]:
    """A result object with the documented fields and well-typed readings."""
    return {
        **{key: draw(readings) for key in [*NUMERIC_KEYS, "powerdc3", "powerdc4"]},
        "feedinpowerM2": draw(readings),
        "inverterSN": draw(st.from_regex(r"[A-Z0-9]{14}", fullmatch=True)),
        "sn": draw(st.from_regex(r"[A-Z0-9]{10}", fullmatch=True)),
        **{key: draw(status_codes) for key in STATUS_KEYS},
        "uploadTime": draw(
            st.none() | any_datetime.map(lambda d: d.strftime("%Y-%m-%d %H:%M:%S"))
        ),
        "utcDateTime": draw(timestamps),
    }


poll_outcomes = st.one_of(
    results().map(lambda result: ("ok", {"json": ok_response(**result)})),
    st.sampled_from(
        [
            ("failed", {"json": OPERATION_FAILED}),
            ("failed", {"exc": aiohttp.ClientConnectionError()}),
            ("failed", {"exc": TimeoutError()}),
            ("failed", {"status": 503}),
            ("failed", {"text": "<html></html>"}),
            ("auth", {"json": TOKEN_REJECTED}),
            ("auth", {"json": SERIAL_REJECTED}),
        ]
    ),
)


def _unexpected_errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelname in ("ERROR", "CRITICAL")
        and not r.getMessage().startswith(COORDINATOR_FAILURE_REPORTS)
    ]


async def _fresh_entry(
    hass: HomeAssistant, entry: MockConfigEntry, api: AiohttpClientMocker
) -> None:
    """(Re)load the entry with a good response so each example starts clean."""
    respond(api, json=ok_response())
    if entry.state is ConfigEntryState.NOT_LOADED:
        await async_setup(hass, entry)
    else:
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


@SHARED_HASS
@given(polls=st.lists(poll_outcomes, min_size=1, max_size=5))
# A "never" sentinel at the end of the date range must not overflow.
@example(polls=[("ok", {"json": ok_response(utcDateTime="9999-12-31T23:59:59Z")})])
# ...nor one at the start whose offset puts it before year 1 in UTC.
@example(polls=[("ok", {"json": ok_response(utcDateTime="0001-01-01T00:00:00+05:00")})])
async def test_property_sensors_follow_every_poll(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    polls: list[tuple[str, dict[str, Any]]],
) -> None:
    """Sensors are unavailable after a failed poll and mirror a good one.

    A refused token or serial stops polling until the user re-authenticates,
    so sensors stay unavailable from then on. No payload or failure may
    break an entity: nothing is logged at ERROR except the coordinator's own
    one-line report of a failed request.
    """
    await _fresh_entry(hass, config_entry, aioclient_mock)
    auth_failed = False

    for outcome, response in polls:
        caplog.clear()
        respond(aioclient_mock, **response)
        await async_poll(hass, freezer)
        auth_failed = auth_failed or outcome == "auth"

        assert _unexpected_errors(caplog) == []
        if outcome != "ok" or auth_failed:
            assert {k: state(hass, k) for k in SENSOR_KEYS} == dict.fromkeys(
                SENSOR_KEYS, STATE_UNAVAILABLE
            )
            continue

        result = response["json"]["result"]
        for key in NUMERIC_KEYS:
            if result[key] is None:
                assert state(hass, key) == STATE_UNKNOWN, key
            else:
                # Home Assistant writes floats with 15 significant digits.
                assert float(state(hass, key)) == pytest.approx(
                    result[key], rel=1e-14
                ), key
        pv_readings = [result[k] for k in PV_KEYS if result[k] is not None]
        if pv_readings:
            assert float(state(hass, "total_solar_power")) == pytest.approx(
                sum(pv_readings), rel=1e-14
            )
        else:
            assert state(hass, "total_solar_power") == STATE_UNKNOWN
        for key in STATUS_KEYS:
            shown = state(hass, key)
            if result[key] in KNOWN_STATUS[key]:
                assert shown == KNOWN_STATUS[key][result[key]], key
            else:
                options = hass.states.get(entity_id(hass, key)).attributes["options"]
                assert shown in [*options, STATE_UNKNOWN], (key, result[key])
        for key in TEXT_KEYS:
            assert state(hass, key) == (result[key] or STATE_UNKNOWN), key
        timestamp = state(hass, "utcDateTime")
        if timestamp != STATE_UNKNOWN:
            assert datetime.fromisoformat(timestamp).tzinfo is not None


@SHARED_HASS
@given(
    body=st.one_of(
        json_values,
        st.fixed_dictionaries(
            {"success": st.just(True), "code": st.just(0)},
            optional={
                "result": st.fixed_dictionaries(
                    {},
                    optional=dict.fromkeys(
                        [*SENSOR_KEYS, "powerdc3", "powerdc4"], json_values
                    ),
                )
            },
        ),
    )
)
# Longer than the 255 characters a Home Assistant state may hold.
@example(body={"success": True, "result": {"sn": "S" * 300}})
async def test_property_any_json_body_is_handled(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    body: Any,
) -> None:
    """Whatever JSON the API sends, no entity breaks.

    Fields with the wrong type are unknown, malformed envelopes make the
    sensors unavailable, and nothing is logged at ERROR beyond the
    coordinator's report of a failed request.
    """
    await _fresh_entry(hass, config_entry, aioclient_mock)
    caplog.clear()
    respond(aioclient_mock, json=body)

    await async_poll(hass, freezer)

    assert _unexpected_errors(caplog) == []
    well_formed = (
        isinstance(body, dict)
        and body.get("success") is True
        and isinstance(body.get("result"), dict)
    )
    for key in SENSOR_KEYS:
        shown = state(hass, key)
        if not well_formed:
            assert shown == STATE_UNAVAILABLE, key
        elif key in NUMERIC_KEYS or key == "total_solar_power":
            assert shown == STATE_UNKNOWN or math.isfinite(float(shown)), key
        else:
            assert shown != STATE_UNAVAILABLE, key


@SHARED_HASS
@given(
    zone=st.sampled_from(
        [
            "Europe/Berlin",
            "Asia/Shanghai",
            "America/Sao_Paulo",
            "Australia/Sydney",
            "Asia/Kolkata",
            "Pacific/Chatham",
            "UTC",
        ]
    ),
    upload=st.datetimes(
        min_value=datetime(2000, 1, 1),
        max_value=datetime(2100, 1, 1),
        timezones=st.just(UTC),
    ).map(lambda d: d.replace(microsecond=0)),
)
async def test_property_utc_date_time_is_upload_instant(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    zone: str,
    upload: datetime,
) -> None:
    """utcDateTime shows the upload instant, matching uploadTime's wall clock.

    Model of the v2 API (confirmed on a UTC+01:00 plant): uploadTime is the
    plant's wall time, utcDateTime the same moment in UTC.
    """
    await _fresh_entry(hass, config_entry, aioclient_mock)
    await hass.config.async_set_time_zone(zone)
    wall = upload.astimezone(ZoneInfo(zone)).replace(tzinfo=None)
    respond(
        aioclient_mock,
        json=ok_response(
            uploadTime=wall.strftime("%Y-%m-%d %H:%M:%S"),
            utcDateTime=upload.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )

    await async_poll(hass, freezer)

    shown = datetime.fromisoformat(state(hass, "utcDateTime"))
    assert shown == upload
    assert shown.astimezone(ZoneInfo(zone)).replace(tzinfo=None) == wall


# Everything str.strip() removes, e.g. the non-breaking spaces a copy from the
# Solax web UI can carry.
whitespace = st.text(
    st.sampled_from([c for c in map(chr, range(sys.maxunicode + 1)) if c.isspace()]),
    max_size=3,
)


@SHARED_HASS
@given(before=whitespace, after=whitespace)
async def test_property_padded_serial_is_same_dongle(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    before: str,
    after: str,
) -> None:
    """Any whitespace pasted around the serial still matches the entry."""
    flow = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        {
            CONF_API_ADDRESS: API_ADDRESS,
            CONF_TOKEN: TOKEN,
            CONF_SERIAL: f"{before}{SERIAL}{after}",
        },
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert aioclient_mock.call_count == 0


# Each example starts a day after the previous one, so the battery series
# restored from the last example never reaches into this one.
EXAMPLE_DAYS = itertools.count(1)
battery_steps = st.lists(
    st.tuples(
        st.sampled_from(["upload", "upload", "upload", "repeat", "fail"]),
        st.floats(min_value=0.5, max_value=20),
        st.none() | st.floats(min_value=-10_000, max_value=10_000),
    ),
    min_size=1,
    max_size=8,
)


@SHARED_HASS
@given(steps=battery_steps)
async def test_property_battery_energy_only_counts_up(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    steps: list[tuple[str, float, float | None]],
) -> None:
    """Battery energy totals never decrease and stay physically plausible.

    Energy only goes to the side the power points to, and the energy added
    never exceeds the largest power seen times the time elapsed.
    """
    await _fresh_entry(hass, config_entry, aioclient_mock)
    start = (
        float(state(hass, "battery_charge_energy")),
        float(state(hass, "battery_discharge_energy")),
    )
    previous = start
    at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=next(EXAMPLE_DAYS))
    first_upload, power = at, None
    powers: list[float] = []

    for kind, gap_minutes, new_power in steps:
        if kind == "fail":
            respond(aioclient_mock, json=OPERATION_FAILED)
        else:
            if kind == "upload":
                at += timedelta(minutes=gap_minutes)
                power = new_power
            if power is not None:
                powers.append(power)
            respond(
                aioclient_mock,
                json=ok_response(
                    batPower=power, utcDateTime=at.strftime("%Y-%m-%dT%H:%M:%SZ")
                ),
            )
        await async_poll(hass, freezer)
        if kind == "fail":
            continue
        current = (
            float(state(hass, "battery_charge_energy")),
            float(state(hass, "battery_discharge_energy")),
        )
        assert current[0] >= previous[0] and current[1] >= previous[1]
        previous = current

    charged, discharged = previous[0] - start[0], previous[1] - start[1]
    if all(p >= 0 for p in powers):
        assert discharged == pytest.approx(0, abs=1e-12)
    if all(p <= 0 for p in powers):
        assert charged == pytest.approx(0, abs=1e-12)
    largest = max((abs(p) for p in powers), default=0)
    elapsed_hours = (at - first_upload).total_seconds() / 3600
    assert charged + discharged <= largest * elapsed_hours / 1000 + 1e-9
