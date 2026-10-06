"""Property-based tests: the integration against arbitrary API behaviour."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import sys
from typing import Any
from zoneinfo import ZoneInfo

from freezegun.api import FrozenDateTimeFactory
from hypothesis import HealthCheck, example, given, settings, strategies as st
import pytest
import requests
import requests_mock as rm

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solax_cloud.const import CONF_SERIAL, CONF_TOKEN, DOMAIN

from .common import (
    API_URL,
    REJECTED_RESPONSE,
    SERIAL,
    TOKEN,
    async_poll,
    async_setup,
    ok_response,
    state,
)

# One Home Assistant instance is shared by all examples of a test; each
# example only feeds it new API responses.
SHARED_HASS = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

# Documented float fields that have a sensor (SolaXCloud User API V1.2, 7.1).
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
# DataUpdateCoordinator's one-line report of a failed request.
COORDINATOR_FAILURE_REPORTS = (
    "Error requesting solax_cloud data",
    "Error fetching solax_cloud data",
)
SENSOR_KEYS = [*NUMERIC_KEYS, "total_solar_power", "sn", "uploadTime", "utcDateTime"]

readings = st.one_of(
    st.none(),
    st.integers(min_value=-(10**7), max_value=10**8),
    st.floats(min_value=-1e7, max_value=1e8, allow_nan=False),
)
any_datetime = st.datetimes()
timestamps = st.one_of(
    st.none(),
    any_datetime.map(lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")),
    any_datetime.map(lambda d: d.isoformat()),
    st.text(max_size=40),
)


@st.composite
def results(draw: st.DrawFn) -> dict[str, Any]:
    """A result object with the documented fields and arbitrary readings."""
    return {
        **{key: draw(readings) for key in [*NUMERIC_KEYS, "powerdc3", "powerdc4"]},
        "feedinpowerM2": draw(readings),
        "sn": draw(st.from_regex(r"[A-Z0-9]{10}", fullmatch=True)),
        "uploadTime": draw(
            st.none() | any_datetime.map(lambda d: d.strftime("%Y-%m-%d %H:%M:%S"))
        ),
        "utcDateTime": draw(timestamps),
    }


poll_outcomes = st.one_of(
    results().map(lambda result: ("ok", {"json": ok_response(**result)})),
    st.sampled_from(
        [
            ("failed", {"json": REJECTED_RESPONSE}),
            ("failed", {"exc": requests.exceptions.ConnectionError}),
            ("failed", {"exc": requests.exceptions.RetryError}),
            ("failed", {"status_code": 503}),
            ("failed", {"status_code": 200, "text": "<html></html>"}),
        ]
    ),
)


@SHARED_HASS
@given(polls=st.lists(poll_outcomes, min_size=1, max_size=5))
# A "never" sentinel at the end of the date range must not overflow.
@example(polls=[("ok", {"json": ok_response(utcDateTime="9999-12-31T23:59:59Z")})])
async def test_property_sensors_follow_every_poll(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    solax_api: rm.Mocker,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    polls: list[tuple[str, dict[str, Any]]],
) -> None:
    """Sensors are unavailable after a failed poll and mirror a good one.

    No payload or failure may break an entity: nothing is logged at ERROR
    except the coordinator's own one-line report of a failed request.
    """
    if config_entry.state is not ConfigEntryState.LOADED:
        await async_setup(hass, config_entry)

    for outcome, response in polls:
        caplog.clear()
        solax_api.get(API_URL, **response)
        await async_poll(hass, freezer)

        unexpected_errors = [
            r.getMessage()
            for r in caplog.records
            if r.levelname in ("ERROR", "CRITICAL")
            and not r.getMessage().startswith(COORDINATOR_FAILURE_REPORTS)
        ]
        assert unexpected_errors == []

        if outcome == "failed":
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
        pv_total = sum(result[k] for k in PV_KEYS if result[k] is not None)
        assert float(state(hass, "total_solar_power")) == pytest.approx(
            pv_total, rel=1e-14
        )
        assert state(hass, "sn") == result["sn"]
        assert state(hass, "uploadTime") == (result["uploadTime"] or STATE_UNKNOWN)
        timestamp = state(hass, "utcDateTime")
        if timestamp != STATE_UNKNOWN:
            assert datetime.fromisoformat(timestamp).tzinfo is not None


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
async def test_property_utc_date_time_shows_upload_wall_time(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    solax_api: rm.Mocker,
    freezer: FrozenDateTimeFactory,
    zone: str,
    upload: datetime,
) -> None:
    """utcDateTime shows the upload at the plant's wall-clock time.

    Model of the API (confirmed for a UTC+01:00 plant in winter and summer):
    uploadTime is the plant's wall time, utcDateTime is that wall time minus
    8 h labelled "Z". Wall times repeated by a DST fall-back are ambiguous in
    that data, so the property compares wall times rather than instants.
    """
    if config_entry.state is not ConfigEntryState.LOADED:
        await async_setup(hass, config_entry)
    await hass.config.async_set_time_zone(zone)
    wall = upload.astimezone(ZoneInfo(zone)).replace(tzinfo=None)
    solax_api.get(
        API_URL,
        json=ok_response(
            uploadTime=wall.strftime("%Y-%m-%d %H:%M:%S"),
            utcDateTime=(wall - timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )

    await async_poll(hass, freezer)

    shown = datetime.fromisoformat(state(hass, "utcDateTime"))
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
    requests_mock: rm.Mocker,
    before: str,
    after: str,
) -> None:
    """Any whitespace pasted around the serial still matches the entry."""
    flow = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_TOKEN: TOKEN, CONF_SERIAL: f"{before}{SERIAL}{after}"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert requests_mock.call_count == 0
