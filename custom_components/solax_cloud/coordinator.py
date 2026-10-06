"""Coordinator for the Solax Cloud integration."""

from __future__ import annotations

from datetime import datetime, timedelta
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
    DEFAULT_CADENCE,
    OFFLINE_POLL,
    OVERDUE_POLL,
    STALE_AFTER,
    cadence,
    is_stale,
    next_poll,
    upload_instant,
)

type SolaxCloudConfigEntry = ConfigEntry[SolaxCloudCoordinator]

# Tell the user once the dongle has not uploaded for this long.
OFFLINE_ISSUE_AFTER = timedelta(hours=3)


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
        )
        self.client = client
        self._last_upload: datetime | None = None
        self._cadence = DEFAULT_CADENCE

    @property
    def stale(self) -> bool:
        """Whether the current data is from an upload too old to be current."""
        return is_stale(self.data, dt_util.utcnow())

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from solax API."""
        try:
            data = await self.client.async_get_realtime_data()
        except SolaxCloudTokenError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="invalid_token",
                translation_placeholders={"error": str(err)},
            ) from err
        except SolaxCloudSerialError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="serial_not_in_account",
                translation_placeholders={
                    "serial": self.config_entry.data[CONF_SERIAL],
                    "error": str(err),
                },
            ) from err
        except SolaxCloudConnectionError as err:
            self.update_interval = self._retry_interval()
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err
        except SolaxCloudError as err:
            self.update_interval = self._retry_interval()
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="api_error",
                translation_placeholders={"error": str(err)},
            ) from err

        now = dt_util.utcnow()
        upload = upload_instant(data)
        if upload is not None and (
            self._last_upload is None or upload > self._last_upload
        ):
            if self._last_upload is not None:
                self._cadence = cadence(self._last_upload, upload)
            self._last_upload = upload
        self.update_interval = next_poll(upload, self._cadence, now)
        self._update_offline_issue(upload, now)
        return data

    def _retry_interval(self) -> timedelta:
        """Retry soon, unless the dongle had already gone quiet."""
        if (
            self._last_upload is not None
            and dt_util.utcnow() - self._last_upload > STALE_AFTER
        ):
            return OFFLINE_POLL
        return OVERDUE_POLL

    def _update_offline_issue(self, upload: datetime | None, now: datetime) -> None:
        issue_id = f"dongle_offline_{self.config_entry.entry_id}"
        if upload is not None and now - upload > OFFLINE_ISSUE_AFTER:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="dongle_offline",
                translation_placeholders={
                    "serial": self.config_entry.data[CONF_SERIAL],
                    "last_upload": _format_upload(upload),
                },
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)


def _format_upload(upload: datetime) -> str:
    """The upload time in local time, or in UTC where that is out of range."""
    try:
        return dt_util.as_local(upload).strftime("%Y-%m-%d %H:%M")
    except OverflowError:
        return upload.strftime("%Y-%m-%d %H:%M UTC")
