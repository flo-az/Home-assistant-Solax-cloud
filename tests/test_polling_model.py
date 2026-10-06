"""Tests for when to poll and when data is stale (no Home Assistant).

The dongle uploads every few minutes (5 on the live install); polling more
often only fetches the same upload again and spends API calls.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given, strategies as st
import pytest

from custom_components.solax_cloud.polling import (
    DEFAULT_CADENCE,
    MAX_CADENCE,
    MIN_CADENCE,
    MIN_POLL,
    OFFLINE_POLL,
    OVERDUE_POLL,
    POLL_AFTER_UPLOAD,
    STALE_AFTER,
    cadence,
    is_stale,
    next_poll,
    upload_instant,
)

UPLOAD = datetime(2026, 10, 6, 13, 23, 4, tzinfo=UTC)
FIVE = timedelta(minutes=5)


@pytest.mark.parametrize(
    ("seconds_since_upload", "expected"),
    [
        # Just fetched a fresh upload: wait for the next one plus a margin.
        (20, FIVE + POLL_AFTER_UPLOAD - timedelta(seconds=20)),
        (300, POLL_AFTER_UPLOAD),
        # Within the margin before the next upload: never faster than MIN_POLL.
        (5 * 60 + 25, MIN_POLL),
        # The next upload is late: check every minute while it may still come.
        (6 * 60, OVERDUE_POLL),
        (STALE_AFTER.total_seconds() - 1, OVERDUE_POLL),
        # Long silent: the dongle is offline; back off.
        (STALE_AFTER.total_seconds() + 1, OFFLINE_POLL),
        (24 * 3600, OFFLINE_POLL),
    ],
)
def test_next_poll_follows_the_upload_rhythm(
    seconds_since_upload: float, expected: timedelta
) -> None:
    """Poll shortly after the next expected upload, not every minute."""
    now = UPLOAD + timedelta(seconds=seconds_since_upload)

    assert next_poll(UPLOAD, FIVE, now) == expected


def test_next_poll_without_upload_time_keeps_checking() -> None:
    """Without a readable upload time the rhythm is unknown: poll every minute."""
    assert next_poll(None, FIVE, UPLOAD) == OVERDUE_POLL


def test_next_poll_for_a_future_upload_waits_one_cadence() -> None:
    """A clock difference must not make us poll in a tight loop or not at all."""
    assert next_poll(UPLOAD + timedelta(minutes=2), FIVE, UPLOAD) == (
        FIVE + POLL_AFTER_UPLOAD
    )


@pytest.mark.parametrize(
    ("previous", "latest", "expected"),
    [
        (UPLOAD - FIVE, UPLOAD, FIVE),
        (UPLOAD - timedelta(minutes=2), UPLOAD, timedelta(minutes=2)),
        # Missed uploads or a restart in between: keep the default.
        (UPLOAD - timedelta(hours=2), UPLOAD, DEFAULT_CADENCE),
        (UPLOAD - timedelta(seconds=10), UPLOAD, DEFAULT_CADENCE),
        (None, UPLOAD, DEFAULT_CADENCE),
    ],
)
def test_cadence_is_learned_from_consecutive_uploads(
    previous: datetime | None, latest: datetime, expected: timedelta
) -> None:
    """The time between two uploads is the cadence, within plausible bounds."""
    assert cadence(previous, latest) == expected


@pytest.mark.parametrize(
    ("utc_date_time", "seconds_later", "stale"),
    [
        ("2026-10-06T13:23:04Z", 60, False),
        ("2026-10-06T13:23:04Z", STALE_AFTER.total_seconds() - 1, False),
        ("2026-10-06T13:23:04Z", STALE_AFTER.total_seconds() + 1, True),
        # Unknown upload time: do not hide the data.
        (None, 10**6, False),
        ("not a time", 10**6, False),
    ],
)
def test_data_is_stale_when_the_upload_is_old(
    utc_date_time: str | None, seconds_later: float, stale: bool
) -> None:
    """An old upload means the dongle stopped sending; its readings are stale."""
    now = UPLOAD + timedelta(seconds=seconds_later)

    assert is_stale({"utcDateTime": utc_date_time}, now) is stale


def test_upload_instant_reads_v2_utc() -> None:
    """v2 sends utcDateTime in real UTC."""
    assert upload_instant({"utcDateTime": "2026-10-06T13:23:04Z"}) == UPLOAD


@given(
    since=st.floats(min_value=-3600, max_value=7 * 24 * 3600),
    cadence_s=st.floats(
        min_value=MIN_CADENCE.total_seconds(), max_value=MAX_CADENCE.total_seconds()
    ),
)
def test_property_poll_interval_is_bounded_and_never_early(
    since: float, cadence_s: float
) -> None:
    """Never a tight loop, never longer than one cadence plus the margin, and
    never before the next upload is due unless it is already overdue."""
    now = UPLOAD + timedelta(seconds=since)
    rhythm = timedelta(seconds=cadence_s)

    interval = next_poll(UPLOAD, rhythm, now)

    assert MIN_POLL <= interval <= max(rhythm + POLL_AFTER_UPLOAD, OFFLINE_POLL)
    if UPLOAD <= now < UPLOAD + rhythm:
        assert now + interval >= UPLOAD + rhythm
