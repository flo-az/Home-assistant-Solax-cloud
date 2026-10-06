"""When to poll Solax Cloud, and when its data is stale.

The dongle uploads on a fixed rhythm (every 5 minutes on known installs).
Polling right after the next expected upload gets each upload with little
delay while spending one API call per upload instead of one per minute.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

DEFAULT_CADENCE = timedelta(minutes=5)
# Plausible time between two consecutive uploads; outside this, uploads were
# missed (or arrived twice) and the default is used.
MIN_CADENCE = timedelta(minutes=1)
MAX_CADENCE = timedelta(minutes=15)

# Give the cloud a moment to have the upload before asking for it.
POLL_AFTER_UPLOAD = timedelta(seconds=30)
MIN_POLL = timedelta(seconds=30)
# While the next upload is late but may still come.
OVERDUE_POLL = timedelta(minutes=1)
# Once the dongle has gone quiet.
OFFLINE_POLL = timedelta(minutes=5)

# Readings from an upload this old no longer describe the present.
STALE_AFTER = timedelta(minutes=20)


def upload_instant(data: dict[str, Any] | None) -> datetime | None:
    """When the data was uploaded; v2 sends utcDateTime in real UTC.

    (v1 sent the plant's wall time read as UTC+8 instead.)
    """
    value = data.get("utcDateTime") if data else None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_util.UTC)
        return parsed.astimezone(dt_util.UTC)
    except (ValueError, OverflowError):
        return None


def cadence(previous: datetime | None, latest: datetime) -> timedelta:
    """The dongle's upload rhythm, from two consecutive uploads."""
    if previous is not None and MIN_CADENCE <= latest - previous <= MAX_CADENCE:
        return latest - previous
    return DEFAULT_CADENCE


def next_poll(
    last_upload: datetime | None, rhythm: timedelta, now: datetime
) -> timedelta:
    """How long to wait before the next poll."""
    if last_upload is None:
        return OVERDUE_POLL
    if last_upload > now:
        # Our clock is behind the cloud's; assume the upload just happened.
        return rhythm + POLL_AFTER_UPLOAD
    due = last_upload + rhythm + POLL_AFTER_UPLOAD
    if due > now:
        return max(due - now, MIN_POLL)
    if now - last_upload <= STALE_AFTER:
        return OVERDUE_POLL
    return OFFLINE_POLL


def is_stale(data: dict[str, Any] | None, now: datetime) -> bool:
    """Whether the data is from an upload too old to describe the present.

    Data without a readable upload time is not considered stale.
    """
    upload = upload_instant(data)
    return upload is not None and now - upload > STALE_AFTER
