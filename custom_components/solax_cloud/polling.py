"""Track the dongle's uploads: when to poll Solax Cloud, and when data is stale.

The dongle uploads on a fixed rhythm (every 5 minutes on known installs). The
cloud stamps each upload with its own clock. The tracker therefore measures
time with a monotonic clock (`now`), which never jumps, and relies on:

- when it first saw each new upload (for staleness),
- differences between the cloud's stamps (for the rhythm), and
- the smallest offset between a stamp and the moment it was seen (for
  scheduling; a constant as long as the cloud's clock does not change).

Home Assistant's wall clock (`wall`) is only used to judge how old the first
upload is and to reject absurd future stamps.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from statistics import median
from typing import Any

from homeassistant.util import dt as dt_util

DEFAULT_CADENCE = timedelta(minutes=5)
# Plausible time between two consecutive uploads.
MIN_CADENCE = timedelta(minutes=1)
MAX_CADENCE = timedelta(minutes=15)

# Polls this close together cannot miss an upload in between (uploads are at
# least MIN_CADENCE apart), so the gap they see is a real cadence.
TRUSTED_POLL_GAP = timedelta(seconds=50)
# While learning or taking a close look at the rhythm.
LEARNING_POLL = timedelta(seconds=30)
# While an upload is late but may still come.
OVERDUE_POLL = timedelta(seconds=45)
# Once the dongle has gone quiet, or no upload time is available.
OFFLINE_POLL = timedelta(minutes=5)
# Give the cloud a moment after the expected upload.
POLL_AFTER_UPLOAD = timedelta(seconds=30)
MIN_POLL = timedelta(seconds=30)

# Learning ends after this long at the latest, with the median gap seen.
LEARNING_LIMIT = timedelta(minutes=30)
# Take a close look at the rhythm this often, in case it changed (or was
# learned wrong, e.g. from alternating missed uploads).
PROBE_EVERY = timedelta(hours=6)
# Recent closely watched gaps kept for the median.
GAPS_KEPT = 5
# A close look lasts until the rhythm is settled, but at most this many
# cadences.
PROBE_CADENCES = 3

# A first upload that looks at most this old by our wall clock is taken as
# just seen; clocks may differ by hours. An older one is stale from the start.
STARTUP_TOLERANCE = timedelta(hours=3)
# Stamps further ahead of our wall clock than this are bogus.
MAX_FUTURE = timedelta(days=1)
# This many consecutive, advancing stamps older than the newest one mean the
# cloud's clock was corrected (or the newest was bogus): adopt them.
RESET_AFTER = 3
# How many recent stamp-to-sighting offsets to keep. Few, so a change of the
# cloud's clock is followed within a few uploads.
OFFSETS_KEPT = 6
# Never assume an upload older than this when placing it on our clock.
MAX_AGE = timedelta(days=30)


def stale_after(cadence: timedelta) -> timedelta:
    """How long without a new upload before readings are stale.

    At least 20 minutes, and always room for one missed upload.
    """
    return max(timedelta(minutes=20), 2 * cadence + timedelta(minutes=5))


def tolerance(cadence: timedelta) -> timedelta:
    """How far two gaps may differ and still be the same cadence.

    Room for stamps jittering by a few seconds and a varying cloud delay.
    """
    return max(timedelta(seconds=15), cadence / 20)


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
    RESET = "reset"  # older stamps adopted: the cloud's clock was corrected
    SAME = "same"
    OLDER = "older"
    BOGUS = "bogus"
    NONE = "none"


@dataclass(frozen=True)
class UploadTracker:
    """What we know about the dongle's uploads (times are monotonic)."""

    started: datetime
    newest: datetime | None = None  # cloud stamp of the newest upload
    seen_at: datetime | None = None  # when it was first seen
    cadence: timedelta = DEFAULT_CADENCE
    confirmed: bool = False
    pending_gap: timedelta | None = None
    pending_count: int = 0
    gaps: tuple[timedelta, ...] = ()  # recent closely watched gaps
    # Sighting time minus stamp: the clock difference plus the cloud's delay,
    # plus however late we looked. Its minimum estimates the first two.
    offsets: tuple[timedelta, ...] = ()
    last_poll: datetime | None = None
    last_probe: datetime | None = None
    # A close look at the rhythm: polling every LEARNING_POLL from then on.
    probing_since: datetime | None = None
    behind: tuple[datetime, ...] = ()  # consecutive older stamps

    @classmethod
    def start(cls, now: datetime) -> UploadTracker:
        """A tracker that has not seen any upload yet."""
        return cls(started=now)


def observe(
    tracker: UploadTracker, stamp: datetime | None, now: datetime, wall: datetime
) -> tuple[UploadTracker, Seen]:
    """Record a successful poll that returned an upload with this stamp."""
    updated, seen = _observe(tracker, stamp, now, wall)
    return _end_learning_if_due(updated, now), seen


def _observe(
    tracker: UploadTracker, stamp: datetime | None, now: datetime, wall: datetime
) -> tuple[UploadTracker, Seen]:
    watched = (
        tracker.last_poll is not None
        and timedelta(0) <= now - tracker.last_poll <= TRUSTED_POLL_GAP
    )
    polled = replace(tracker, last_poll=now)
    if stamp is None:
        return polled, Seen.NONE
    if stamp > wall + MAX_FUTURE:
        return polled, Seen.BOGUS
    if polled.newest is None:
        age = wall - stamp
        if age > STARTUP_TOLERANCE:
            # Stale from the start; place the upload that far in our past.
            return (
                replace(polled, newest=stamp, seen_at=now - min(age, MAX_AGE)),
                Seen.NEW,
            )
        return (
            replace(polled, newest=stamp, seen_at=now, offsets=(now - stamp,)),
            Seen.NEW,
        )
    if stamp == polled.newest:
        return replace(polled, behind=()), Seen.SAME
    if stamp < polled.newest:
        return _older(polled, stamp, now)
    return _newer(polled, stamp, now, watched), Seen.NEW


def _older(
    tracker: UploadTracker, stamp: datetime, now: datetime
) -> tuple[UploadTracker, Seen]:
    """An upload older than the newest: a stale replica, or a clock fix."""
    if tracker.behind and stamp > tracker.behind[-1]:
        behind = (*tracker.behind, stamp)
    else:
        behind = (stamp,)
    if len(behind) < RESET_AFTER:
        return replace(tracker, behind=behind), Seen.OLDER
    return (
        replace(
            tracker,
            newest=stamp,
            seen_at=now,
            offsets=(now - stamp,),
            pending_gap=None,
            pending_count=0,
            behind=(),
        ),
        Seen.RESET,
    )


def _newer(
    tracker: UploadTracker, stamp: datetime, now: datetime, watched: bool
) -> UploadTracker:
    """A new upload: update the offset and, if closely watched, the cadence."""
    assert tracker.newest is not None
    gap = stamp - tracker.newest
    updated = replace(tracker, newest=stamp, seen_at=now, behind=())
    if updated.pending_gap is not None and gap < updated.pending_gap - tolerance(
        updated.pending_gap
    ):
        # Uploads were closer than the suspected new cadence: it was wrong
        # (gaps are always whole multiples of the real cadence).
        updated = replace(updated, pending_gap=None, pending_count=0)
    offset = now - stamp
    # A closely watched sighting measures the offset. Any other sighting is
    # only an upper bound: it may lower the estimate but never raise it, or
    # our own scheduling margin would feed back in.
    if watched or not updated.offsets or offset < min(updated.offsets):
        updated = replace(updated, offsets=(*updated.offsets, offset)[-OFFSETS_KEPT:])
    if watched and MIN_CADENCE - tolerance(
        MIN_CADENCE
    ) <= gap <= MAX_CADENCE + tolerance(MAX_CADENCE):
        updated = _learn(updated, gap)
    if updated.probing_since is not None:
        settled = watched and updated.pending_gap is None
        too_long = now - updated.probing_since > PROBE_CADENCES * updated.cadence
        if settled or too_long:
            updated = replace(updated, probing_since=None, last_probe=now)
    elif updated.confirmed and not watched and not _fits(gap, updated.cadence):
        # A gap that no whole number of cadences explains: the rhythm
        # changed. Look closely right away.
        updated = replace(updated, probing_since=now)
    elif (
        updated.confirmed
        and now - (updated.last_probe or updated.started) >= PROBE_EVERY
    ):
        # Periodic close look, from just before the next expected upload.
        expected = stamp + updated.cadence + min(updated.offsets)
        updated = replace(updated, probing_since=expected - LEARNING_POLL)
    return updated


def _fits(gap: timedelta, cadence: timedelta) -> bool:
    """Whether a gap is a whole number of cadences."""
    multiple = max(round(gap / cadence), 1)
    return abs(gap - multiple * cadence) <= tolerance(cadence) * multiple


def _learn(tracker: UploadTracker, gap: timedelta) -> UploadTracker:
    """A closely watched gap between consecutive uploads: confirm or change.

    One odd gap may be a missed upload, so a change needs consistent gaps:
    two, or three when the gap is a multiple of the current cadence (which
    is what missed uploads look like).
    """
    tracker = replace(tracker, gaps=(*tracker.gaps, gap)[-GAPS_KEPT:])
    if abs(gap - tracker.cadence) <= tolerance(tracker.cadence):
        return replace(tracker, confirmed=True, pending_gap=None, pending_count=0)
    if tracker.pending_gap is not None and abs(gap - tracker.pending_gap) <= tolerance(
        gap
    ):
        count = tracker.pending_count + 1
        multiple = round(gap / tracker.cadence)
        needed = (
            3
            if tracker.confirmed and multiple >= 2 and _fits(gap, tracker.cadence)
            else 2
        )
        if count >= needed:
            return replace(
                tracker,
                cadence=(gap + tracker.pending_gap) / 2,
                confirmed=True,
                pending_gap=None,
                pending_count=0,
            )
        return replace(tracker, pending_count=count)
    return replace(tracker, pending_gap=gap, pending_count=1)


def _end_learning_if_due(tracker: UploadTracker, now: datetime) -> UploadTracker:
    """Stop learning after LEARNING_LIMIT with the typical measured gap.

    Without any measured gap it keeps learning: then there are no uploads to
    learn from, data goes stale and polling backs off anyway.
    """
    if tracker.confirmed or not tracker.gaps or now - tracker.started < LEARNING_LIMIT:
        return tracker
    # Missed uploads only make gaps longer: use the gaps near the shortest.
    shortest = min(tracker.gaps)
    near = [g for g in tracker.gaps if g - shortest <= 2 * tolerance(shortest)]
    return replace(
        tracker,
        cadence=timedelta(seconds=median(g.total_seconds() for g in near)),
        confirmed=True,
        pending_gap=None,
        pending_count=0,
    )


def is_stale(tracker: UploadTracker, now: datetime) -> bool:
    """Whether no new upload has been seen for too long.

    Without any usable upload time, data is not considered stale.
    """
    # Until the cadence is known, allow for the slowest plausible one.
    cadence = tracker.cadence if tracker.confirmed else MAX_CADENCE
    return tracker.seen_at is not None and now - tracker.seen_at > stale_after(cadence)


def next_poll(tracker: UploadTracker, now: datetime) -> timedelta:
    """How long to wait before the next poll."""
    if tracker.newest is None or tracker.seen_at is None:
        if now - tracker.started > stale_after(DEFAULT_CADENCE):
            return OFFLINE_POLL
        return OVERDUE_POLL
    if is_stale(tracker, now):
        return OFFLINE_POLL
    if not tracker.confirmed:
        quiet = now - tracker.seen_at > MAX_CADENCE + POLL_AFTER_UPLOAD
        # No upload for a whole maximal cadence: the dongle is quiet.
        return OFFLINE_POLL if quiet else LEARNING_POLL
    # When the next upload should become visible, on our clock: its stamp
    # plus the smallest offset seen (clock difference + cloud delay).
    offset = min(tracker.offsets, default=tracker.seen_at - tracker.newest)
    expected = tracker.newest + tracker.cadence + offset
    if tracker.probing_since is not None:
        due, late = tracker.probing_since, LEARNING_POLL
    else:
        due, late = expected + POLL_AFTER_UPLOAD, OVERDUE_POLL
    if due <= now:
        return late
    return min(max(due - now, MIN_POLL), tracker.cadence + POLL_AFTER_UPLOAD)
