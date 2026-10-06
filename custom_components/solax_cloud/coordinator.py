"""Coordinator for the Solax Cloud integration."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    SolaxCloudClient,
    SolaxCloudConnectionError,
    SolaxCloudError,
    SolaxCloudSerialError,
    SolaxCloudTokenError,
)
from .const import CONF_SERIAL, DOMAIN, LOGGER
from .polling import (
    OFFLINE_POLL,
    OVERDUE_POLL,
    Seen,
    UploadTracker,
    is_stale,
    next_poll,
    observe,
    upload_instant,
)

type SolaxCloudConfigEntry = ConfigEntry[SolaxCloudCoordinator]

# Origin for expressing the monotonic clock as datetimes (arbitrary).
MONOTONIC_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)

# Tell the user once no new upload has been seen for this long. Shorter
# would flag PV-only dongles that lose power every night.
OFFLINE_ISSUE_AFTER = timedelta(hours=24)


def offline_issue_id(entry: ConfigEntry) -> str:
    """Repair issue id for an entry's dongle not uploading."""
    return f"dongle_offline_{entry.entry_id}"


class SolaxCloudCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls the realtime data of one dongle, in step with its uploads."""

    config_entry: SolaxCloudConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SolaxCloudConfigEntry,
        client: SolaxCloudClient,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=OVERDUE_POLL,
            # Write states on every poll even if the data did not change: live
            # sensors turn unavailable when no new upload arrives, and only
            # re-evaluate that when their state is written.
            always_update=True,
        )
        self.client = client
        self.tracker = UploadTracker.start(self._monotonic())
        # How the latest poll's upload compared with what we had.
        self.last_seen = Seen.NONE

    def _monotonic(self) -> datetime:
        """The event loop's monotonic clock, which never jumps, as a datetime."""
        return MONOTONIC_EPOCH + timedelta(seconds=self.hass.loop.time())

    @property
    def stale(self) -> bool:
        """Whether no new upload has been seen for too long."""
        return is_stale(self.tracker, self._monotonic())

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from solax API."""
        try:
            data = await self.client.async_get_realtime_data()
        except SolaxCloudTokenError as err:
            self.async_clear_offline_issue()
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="invalid_token",
                translation_placeholders={"error": str(err)},
            ) from err
        except SolaxCloudSerialError as err:
            self.async_clear_offline_issue()
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="serial_not_in_account",
                translation_placeholders={
                    "serial": self.config_entry.data[CONF_SERIAL],
                    "error": str(err),
                },
            ) from err
        except SolaxCloudConnectionError as err:
            self._schedule_retry()
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err
        except SolaxCloudError as err:
            self._schedule_retry()
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="api_error",
                translation_placeholders={"error": str(err)},
            ) from err

        now = self._monotonic()
        self.tracker, seen = observe(
            self.tracker, upload_instant(data), now, dt_util.utcnow()
        )
        self.last_seen = seen
        self.update_interval = next_poll(self.tracker, now)
        self._update_offline_issue(now)
        if seen is Seen.OLDER and self.data is not None:
            # A stale cloud replica answered; keep the newer data we have.
            return self.data
        return data

    def _schedule_retry(self) -> None:
        """Retry soon, unless the dongle had already gone quiet."""
        # A failed poll breaks the chain of closely watched uploads.
        self.tracker = replace(self.tracker, last_poll=None)
        self.update_interval = OFFLINE_POLL if self.stale else OVERDUE_POLL

    def _update_offline_issue(self, now: datetime) -> None:
        seen_at, newest = self.tracker.seen_at, self.tracker.newest
        if (
            seen_at is not None
            and newest is not None
            and now - seen_at > OFFLINE_ISSUE_AFTER
        ):
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                offline_issue_id(self.config_entry),
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="dongle_offline",
                # The last upload as stamped by Solax Cloud.
                translation_placeholders={"last_upload": _format_time(newest)},
            )
        else:
            self.async_clear_offline_issue()

    def async_clear_offline_issue(self) -> None:
        """Remove the offline notice (data returned, token problem, unload)."""
        ir.async_delete_issue(self.hass, DOMAIN, offline_issue_id(self.config_entry))


def _format_time(moment: datetime) -> str:
    """A time in local time, or in UTC where that is out of range."""
    try:
        return dt_util.as_local(moment).strftime("%Y-%m-%d %H:%M")
    except OverflowError:
        return moment.strftime("%Y-%m-%d %H:%M UTC")
