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

from .common import API_URL, OPERATION_FAILED, async_setup, ok_response, state

# The live sample's upload; the dongle uploads every 5 minutes after it.
FIRST_UPLOAD = datetime(2026, 10, 6, 13, 23, 4, tzinfo=UTC)
FIVE = timedelta(minutes=5)


def latest_upload_at(now: datetime, last: datetime | None = None) -> datetime:
    """The newest upload on the 5-minute grid at `now` (or frozen at `last`)."""
    newest = FIRST_UPLOAD + FIVE * int((now - FIRST_UPLOAD) / FIVE)
    return newest if last is None else min(newest, last)


def cloud(
    aioclient_mock: AiohttpClientMocker, last: datetime | None = None
) -> list[datetime]:
    """Serve the newest upload at request time; return the list of request times."""
    requests: list[datetime] = []

    async def answer(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        now = dt_util.utcnow()
        requests.append(now)
        upload = latest_upload_at(now, last)
        return AiohttpClientMockResponse(
            method,
            url,
            json=ok_response(
                acpower=float(int((upload - FIRST_UPLOAD) / FIVE)),
                utcDateTime=upload.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
        )

    aioclient_mock.clear_requests()
    aioclient_mock.post(API_URL, side_effect=answer)
    return requests


async def minutes_pass(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, minutes: int
) -> None:
    """Let time pass in 10-second steps, firing whatever polls come due."""
    for _ in range(minutes * 6):
        freezer.tick(timedelta(seconds=10))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)


async def test_polls_once_per_upload_and_shows_it_promptly(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """30 minutes: about one call per upload instead of 30, each shown within a minute."""
    requests = cloud(aioclient_mock)
    await async_setup(hass, config_entry)

    for upload_number in range(1, 7):
        upload = FIRST_UPLOAD + FIVE * upload_number
        await minutes_pass(hass, freezer, 5)
        # acpower carries the upload number; checked once that upload is due.
        assert dt_util.utcnow() >= upload
        assert dt_util.utcnow() - upload < timedelta(minutes=1)
        assert state(hass, "acpower") == f"{float(upload_number)}"

    assert len(requests) <= 8


async def test_backs_off_once_the_dongle_goes_quiet(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Without new uploads: every minute while one may come, then every 5."""
    requests = cloud(aioclient_mock, last=FIRST_UPLOAD)
    await async_setup(hass, config_entry)

    await minutes_pass(hass, freezer, 20)
    early = len(requests)
    await minutes_pass(hass, freezer, 30)

    # Overdue from minute ~5 to 20: about one call a minute.
    assert 12 <= early <= 18
    # Offline afterwards: about one call per 5 minutes.
    assert len(requests) - early <= 7


async def test_failed_poll_is_retried_after_a_minute(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """After a failed request, retry every minute rather than every upload."""
    aioclient_mock.post(API_URL, json=ok_response())
    await async_setup(hass, config_entry)
    aioclient_mock.clear_requests()
    aioclient_mock.post(API_URL, json=OPERATION_FAILED)

    # The first poll is due 5.5 minutes after the upload, then one a minute.
    await minutes_pass(hass, freezer, 8)

    assert 3 <= aioclient_mock.call_count <= 5


LIVE_KEYS = ["acpower", "total_solar_power", "batPower", "soc", "inverterStatus"]
KEPT_KEYS = ["yieldtotal", "feedinenergy", "uploadTime", "utcDateTime", "inverterSN"]


@pytest.mark.parametrize(
    ("minutes_old", "live_available"),
    [(19, True), (21, False)],
)
async def test_live_readings_are_unavailable_when_the_upload_is_old(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    minutes_old: int,
    live_available: bool,
) -> None:
    """Power, charge level and status from an old upload are not shown as current.

    Totals, serials and the upload time stay, so the cause is visible.
    """
    freezer.move_to(FIRST_UPLOAD + timedelta(minutes=minutes_old))
    aioclient_mock.post(API_URL, json=ok_response())

    await async_setup(hass, config_entry)

    live = {key: state(hass, key) != STATE_UNAVAILABLE for key in LIVE_KEYS}
    kept = {key: state(hass, key) != STATE_UNAVAILABLE for key in KEPT_KEYS}
    assert live == dict.fromkeys(LIVE_KEYS, live_available)
    assert kept == dict.fromkeys(KEPT_KEYS, True)


async def test_dongle_offline_raises_and_clears_a_repair_issue(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """After 3 hours without an upload the user is told; it clears on its own."""
    freezer.move_to(FIRST_UPLOAD + timedelta(hours=3, minutes=1))
    aioclient_mock.post(API_URL, json=ok_response())
    await async_setup(hass, config_entry)

    issue_id = f"dongle_offline_{config_entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == "dongle_offline"

    fresh = dt_util.utcnow()
    aioclient_mock.clear_requests()
    aioclient_mock.post(
        API_URL, json=ok_response(utcDateTime=fresh.strftime("%Y-%m-%dT%H:%M:%SZ"))
    )
    await minutes_pass(hass, freezer, 6)

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    assert state(hass, "acpower") == "3433.0"


async def test_regression_placeholder_upload_date_does_not_fail_the_poll(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Regression: a year-1 upload time made the offline notice crash the poll.

    Showing it in local time overflowed (before year 1 west of UTC), so the
    whole poll failed and its data was dropped. Found by a property test.
    """
    aioclient_mock.post(API_URL, json=ok_response(utcDateTime="0001-01-01T00:01:00Z"))

    await async_setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"dongle_offline_{config_entry.entry_id}"
    )
    assert issue is not None
    assert issue.translation_placeholders["last_upload"] == "0001-01-01 00:01 UTC"
