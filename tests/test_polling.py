"""Tests for polling in step with the dongle's uploads, and stale data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
from yarl import URL

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.solax_cloud.const import DOMAIN

from .common import (
    API_URL,
    OPERATION_FAILED,
    TOKEN_REJECTED,
    async_setup,
    ok_response,
    respond,
    state,
)

# The live sample's upload; the simulated dongle uploads on a grid after it.
FIRST_UPLOAD = datetime(2026, 10, 6, 13, 23, 4, tzinfo=UTC)
MIN = timedelta(minutes=1)
LIVE_KEYS = ["acpower", "total_solar_power", "batPower", "soc", "inverterStatus"]
KEPT_KEYS = ["yieldtotal", "feedinenergy", "uploadTime", "utcDateTime", "inverterSN"]


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def cloud(
    aioclient_mock: AiohttpClientMocker,
    cadence: timedelta = 5 * MIN,
    last: datetime | None = None,
    behind: timedelta = timedelta(0),
) -> list[datetime]:
    """Serve the newest upload at request time; return the request times.

    Uploads are `cadence` apart from FIRST_UPLOAD (none after `last`), and
    stamped by a cloud clock `behind` ours. acpower is the upload's number.
    """
    requests: list[datetime] = []

    async def answer(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        now = dt_util.utcnow()
        requests.append(now)
        number = int((now - FIRST_UPLOAD) / cadence)
        upload = FIRST_UPLOAD + cadence * number
        if last is not None and upload > last:
            upload, number = last, int((last - FIRST_UPLOAD) / cadence)
        return AiohttpClientMockResponse(
            method,
            url,
            json=ok_response(acpower=float(number), utcDateTime=stamp(upload - behind)),
        )

    aioclient_mock.clear_requests()
    aioclient_mock.post(API_URL, side_effect=answer)
    return requests


async def time_passes(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, duration: timedelta
) -> None:
    """Let time pass in 10-second steps, firing whatever polls come due."""
    for _ in range(int(duration / timedelta(seconds=10))):
        freezer.tick(timedelta(seconds=10))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)


def live_available(hass: HomeAssistant) -> dict[str, bool]:
    return {key: state(hass, key) != STATE_UNAVAILABLE for key in LIVE_KEYS}


@pytest.mark.parametrize("minutes", [5, 2])
async def test_polls_once_per_upload_once_the_rhythm_is_learned(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    minutes: int,
) -> None:
    """After learning: about one request per upload, each shown within a minute."""
    cadence = minutes * MIN
    requests = cloud(aioclient_mock, cadence)
    await async_setup(hass, config_entry)
    await time_passes(hass, freezer, 4 * cadence)  # learning
    learned = len(requests)

    uploads = 0
    for _ in range(int(30 * MIN / cadence)):
        await time_passes(hass, freezer, cadence)
        number = int((dt_util.utcnow() - FIRST_UPLOAD) / cadence)
        upload = FIRST_UPLOAD + cadence * number
        uploads += 1
        if dt_util.utcnow() - upload >= MIN:
            assert state(hass, "acpower") == f"{float(number)}"

    # One per upload, plus an occasional re-measuring probe.
    assert len(requests) - learned <= uploads + 6


async def test_backs_off_once_the_dongle_goes_quiet(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Without new uploads: close polling while one may come, then every 5 min."""
    requests = cloud(aioclient_mock, last=FIRST_UPLOAD)
    await async_setup(hass, config_entry)

    await time_passes(hass, freezer, 21 * MIN)
    early = len(requests)
    await time_passes(hass, freezer, 30 * MIN)

    assert early <= 45
    assert len(requests) - early <= 7


async def test_failed_poll_is_retried_soon(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """After a failed request, retry within a minute."""
    respond(aioclient_mock, json=ok_response())
    await async_setup(hass, config_entry)
    respond(aioclient_mock, json=OPERATION_FAILED)

    await time_passes(hass, freezer, 3 * MIN)

    assert 3 <= aioclient_mock.call_count <= 7


async def test_failed_poll_is_retried_slowly_once_the_dongle_is_quiet(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A dead cloud and a quiet dongle: one try every 5 minutes, not every minute."""
    cloud(aioclient_mock, last=FIRST_UPLOAD)
    await async_setup(hass, config_entry)
    await time_passes(hass, freezer, 25 * MIN)
    respond(aioclient_mock, json=OPERATION_FAILED)

    await time_passes(hass, freezer, 20 * MIN)

    assert aioclient_mock.call_count <= 5


async def test_live_readings_go_unavailable_when_uploads_stop(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The cloud keeps answering with the last upload: after 20 minutes without
    a new one, live readings are unavailable; totals and upload time stay."""
    cloud(aioclient_mock, last=FIRST_UPLOAD)
    await async_setup(hass, config_entry)

    await time_passes(hass, freezer, 19 * MIN)
    assert live_available(hass) == dict.fromkeys(LIVE_KEYS, True)

    await time_passes(hass, freezer, 6 * MIN)
    assert live_available(hass) == dict.fromkeys(LIVE_KEYS, False)
    assert {k: state(hass, k) != STATE_UNAVAILABLE for k in KEPT_KEYS} == dict.fromkeys(
        KEPT_KEYS, True
    )


@pytest.mark.parametrize(
    ("age", "live"),
    [(timedelta(minutes=50), True), (timedelta(hours=2), False)],
    ids=["recent-by-our-clock", "hours-old"],
)
async def test_first_upload_after_start(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    age: timedelta,
    live: bool,
) -> None:
    """Clocks may differ: up to an hour old is trusted at start, hours are not."""
    respond(aioclient_mock, json=ok_response(utcDateTime=stamp(dt_util.utcnow() - age)))

    await async_setup(hass, config_entry)

    assert live_available(hass) == dict.fromkeys(LIVE_KEYS, live)


async def test_regression_cloud_clock_offset_does_not_hide_live_readings(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Regression: stamps 30 minutes behind our clock made live readings
    permanently unavailable although uploads kept coming."""
    cloud(aioclient_mock, behind=30 * MIN)
    await async_setup(hass, config_entry)

    await time_passes(hass, freezer, 40 * MIN)

    assert live_available(hass) == dict.fromkeys(LIVE_KEYS, True)


async def test_older_upload_keeps_the_newer_data(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A stale cloud replica answering with an older upload changes nothing."""
    respond(aioclient_mock, json=ok_response())
    await async_setup(hass, config_entry)
    respond(
        aioclient_mock,
        json=ok_response(acpower=1.0, utcDateTime=stamp(FIRST_UPLOAD - 5 * MIN)),
    )

    await time_passes(hass, freezer, 6 * MIN)

    assert aioclient_mock.call_count >= 1
    assert state(hass, "acpower") == "3433.0"
    assert state(hass, "utcDateTime") == "2026-10-06T13:23:04+00:00"


async def test_without_upload_times_it_backs_off(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """No usable upload time at all: poll every 5 minutes after a while."""
    respond(aioclient_mock, json=ok_response(utcDateTime=None))
    await async_setup(hass, config_entry)
    await time_passes(hass, freezer, 25 * MIN)
    before = aioclient_mock.call_count

    await time_passes(hass, freezer, 20 * MIN)

    assert aioclient_mock.call_count - before <= 5


def _offline_issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"dongle_offline_{entry.entry_id}"
    )


async def test_dongle_offline_notice_comes_and_goes(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """After a day without uploads the user is told; it clears on its own."""
    old = dt_util.utcnow() - timedelta(hours=25)
    respond(aioclient_mock, json=ok_response(utcDateTime=stamp(old)))
    await async_setup(hass, config_entry)

    issue = _offline_issue(hass, config_entry)
    assert issue is not None
    assert issue.translation_key == "dongle_offline"
    assert set(issue.translation_placeholders or {}) == {"last_upload"}

    respond(aioclient_mock, json=ok_response(utcDateTime=stamp(dt_util.utcnow())))
    await time_passes(hass, freezer, 6 * MIN)

    assert _offline_issue(hass, config_entry) is None
    assert state(hass, "acpower") == "3433.0"


async def test_no_offline_notice_after_a_night_without_uploads(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Dongles powered by the inverter go quiet every night; that is no issue."""
    old = dt_util.utcnow() - timedelta(hours=12)
    respond(aioclient_mock, json=ok_response(utcDateTime=stamp(old)))

    await async_setup(hass, config_entry)

    assert _offline_issue(hass, config_entry) is None


@pytest.mark.parametrize("how", ["unload", "token-refused"])
async def test_offline_notice_is_removed_when_it_no_longer_applies(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    how: str,
) -> None:
    """Unloading the entry or a token problem removes the offline notice."""
    old = dt_util.utcnow() - timedelta(hours=25)
    respond(aioclient_mock, json=ok_response(utcDateTime=stamp(old)))
    await async_setup(hass, config_entry)
    assert _offline_issue(hass, config_entry) is not None

    if how == "unload":
        await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()
    else:
        respond(aioclient_mock, json=TOKEN_REJECTED)
        await time_passes(hass, freezer, 6 * MIN)

    assert _offline_issue(hass, config_entry) is None


async def test_regression_placeholder_upload_date_does_not_fail_the_poll(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Regression: a year-1 upload time made the offline notice crash the poll.

    Showing it in local time overflowed (before year 1 west of UTC), so the
    whole poll failed and its data was dropped. Found by a property test.
    """
    respond(aioclient_mock, json=ok_response(utcDateTime="0001-01-01T00:01:00Z"))

    await async_setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    issue = _offline_issue(hass, config_entry)
    assert issue is not None
    assert issue.translation_placeholders == {"last_upload": "0001-01-01 00:01 UTC"}
