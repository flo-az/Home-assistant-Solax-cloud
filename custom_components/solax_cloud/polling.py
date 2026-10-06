"""Track the dongle's uploads: when to poll Solax Cloud, and when data is stale.

The dongle uploads on a fixed rhythm (every 5 minutes on known installs). The
cloud stamps each upload with its own clock, which may differ from Home
Assistant's. Everything here therefore rests on:

- when Home Assistant first saw each new upload (for staleness), and
- differences between the cloud's stamps (for the rhythm), plus the smallest
  observed offset between a stamp and the moment we saw it (for scheduling).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from homeassistant.util import dt as dt_util

DEFAULT_CADENCE = timedelta(minutes=5)
# Plausible time between two consecutive uploads.
MIN_CADENCE = timedelta(minutes=1)
MAX_CADENCE = timedelta(minutes=15)
# Two gaps this close are the same cadence.
CADENCE_TOLERANCE = timedelta(seconds=10)

# Polls this close together cannot miss an upload in between (uploads are at
# least MIN_CADENCE apart), so the gap they see is a real cadence.
TRUSTED_POLL_GAP = timedelta(seconds=50)
# While the cadence is not yet confirmed.
LEARNING_POLL = timedelta(seconds=30)
# While an upload is late but may still come.
OVERDUE_POLL = timedelta(seconds=45)
# Once the dongle has gone quiet, or no upload time is available.
OFFLINE_POLL = timedelta(minutes=5)
# Give the cloud a moment after the expected upload.
POLL_AFTER_UPLOAD = timedelta(seconds=30)
MIN_POLL = timedelta(seconds=30)

# A first upload that looks at most this old by our clock is taken as just
# seen; clocks may differ. An older one is stale from the start.
STARTUP_TOLERANCE = timedelta(hours=1)
# Stamps further ahead of our clock than this are bogus.
MAX_FUTURE = timedelta(days=1)
# How many recent stamp-to-sighting offsets to keep. Few, so a jump of our
# clock is followed within a few uploads.
OFFSETS_KEPT = 6


def stale_after(cadence: timedelta) -> timedelta:
    """How long without a new upload before readings are stale.

    At least 20 minutes, and always room for one missed upload.
    """
    return max(timedelta(minutes=20), 2 * cadence + timedelta(minutes=5))


def upload_instant(data: dict[str, Any] | None) -> datetime | None:
    """When the data was uploaded, by the cloud's clock (v2: real UTC).

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


class Seen(Enum):
    """What a poll's upload stamp was, relative to what we had."""

    NEW = "new"
    SAME = "same"
    OLDER = "older"
    BOGUS = "bogus"
    NONE = "none"


@dataclass(frozen=True)
class UploadTracker:
    """What we know about the dongle's uploads."""

    started: datetime
    newest: datetime | None = None  # cloud stamp of the newest upload
    seen_at: datetime | None = None  # our time when it was first seen
    cadence: timedelta = DEFAULT_CADENCE
    confirmed: bool = False
    pending_gap: timedelta | None = None
    pending_count: int = 0
    # Our sighting time minus stamp: the clock difference plus the cloud's
    # delay, plus however late we looked. Its minimum estimates the first two.
    offsets: tuple[timedelta, ...] = ()
    last_poll: datetime | None = None

    @classmethod
    def start(cls, now: datetime) -> UploadTracker:
        """A tracker that has not seen any upload yet."""
        return cls(started=now)


def observe(
    tracker: UploadTracker, stamp: datetime | None, now: datetime
) -> tuple[UploadTracker, Seen]:
    """Record a successful poll that returned an upload with this stamp."""
    polled = replace(tracker, last_poll=now)
    if stamp is None:
        return polled, Seen.NONE
    if stamp > now + MAX_FUTURE:
        return polled, Seen.BOGUS
    if tracker.newest is None:
        if now - stamp > STARTUP_TOLERANCE:
            return replace(polled, newest=stamp, seen_at=min(stamp, now)), Seen.NEW
        return (
            replace(polled, newest=stamp, seen_at=now, offsets=(now - stamp,)),
            Seen.NEW,
        )
    if stamp == tracker.newest:
        return polled, Seen.SAME
    if stamp < tracker.newest:
        return polled, Seen.OLDER

    gap = stamp - tracker.newest
    updated = replace(polled, newest=stamp, seen_at=now)
    if (
        updated.pending_gap is not None
        and gap < updated.pending_gap - CADENCE_TOLERANCE
    ):
        # Uploads were closer than the suspected new cadence: it was wrong
        # (gaps are always whole multiples of the real cadence).
        updated = replace(updated, pending_gap=None, pending_count=0)
    watched = (
        tracker.last_poll is not None and now - tracker.last_poll <= TRUSTED_POLL_GAP
    )
    offset = now - stamp
    # A closely watched sighting measures the offset. Any other sighting is
    # only an upper bound: it may lower the estimate (our clock jumped back)
    # but never raise it, or our own scheduling margin would feed back in.
    if watched or not tracker.offsets or offset < min(tracker.offsets):
        updated = replace(updated, offsets=(*tracker.offsets, offset)[-OFFSETS_KEPT:])
    if watched and MIN_CADENCE <= gap <= MAX_CADENCE:
        updated = _learn(updated, gap)
    return updated, Seen.NEW


def _is_multiple(gap: timedelta, cadence: timedelta) -> bool:
    """Whether a gap is 2, 3, ... cadences: likely missed uploads."""
    multiple = round(gap / cadence)
    return multiple >= 2 and abs(gap - multiple * cadence) <= CADENCE_TOLERANCE


def _learn(tracker: UploadTracker, gap: timedelta) -> UploadTracker:
    """A closely watched gap between consecutive uploads: confirm or change.

    One odd gap may be a missed upload, so a change needs consistent gaps:
    two, or three when the gap is a multiple of the current cadence (which
    is what missed uploads look like).
    """
    if abs(gap - tracker.cadence) <= CADENCE_TOLERANCE:
        return replace(tracker, confirmed=True, pending_gap=None, pending_count=0)
    if (
        tracker.pending_gap is not None
        and abs(gap - tracker.pending_gap) <= CADENCE_TOLERANCE
    ):
        count = tracker.pending_count + 1
        needed = 3 if tracker.confirmed and _is_multiple(gap, tracker.cadence) else 2
        if count >= needed:
            return replace(
                tracker, cadence=gap, confirmed=True, pending_gap=None, pending_count=0
            )
        return replace(tracker, pending_count=count)
    return replace(tracker, pending_gap=gap, pending_count=1)


def is_stale(tracker: UploadTracker, now: datetime) -> bool:
    """Whether no new upload has been seen for too long.

    Without any usable upload time, data is not considered stale.
    """
    return tracker.seen_at is not None and now - tracker.seen_at > stale_after(
        tracker.cadence
    )


def next_poll(tracker: UploadTracker, now: datetime) -> timedelta:
    """How long to wait before the next poll."""
    if tracker.newest is None or tracker.seen_at is None:
        if now - tracker.started > stale_after(DEFAULT_CADENCE):
            return OFFLINE_POLL
        return OVERDUE_POLL
    if is_stale(tracker, now):
        return OFFLINE_POLL
    if not tracker.confirmed:
        return LEARNING_POLL
    # When the next upload should become visible, on our clock: its stamp
    # plus the smallest offset seen (clock difference + cloud delay). A
    # confirmed cadence always comes with a measured offset.
    offset = min(tracker.offsets, default=tracker.seen_at - tracker.newest)
    due = tracker.newest + tracker.cadence + offset + POLL_AFTER_UPLOAD
    if due > now:
        return max(due - now, MIN_POLL)
    return OVERDUE_POLL
