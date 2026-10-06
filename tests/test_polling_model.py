"""Tests for tracking the dongle's uploads: when to poll, and when data is stale.

The cloud stamps each upload with its own clock; Home Assistant's clock may
differ. Staleness and scheduling therefore rest on when Home Assistant first
saw each new upload, and on differences between the cloud's stamps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from hypothesis import given, settings, strategies as st
import pytest

from custom_components.solax_cloud.polling import (
    DEFAULT_CADENCE,
    LEARNING_POLL,
    MAX_FUTURE,
    MIN_POLL,
    OFFLINE_POLL,
    OVERDUE_POLL,
    STARTUP_TOLERANCE,
    Seen,
    UploadTracker,
    is_stale,
    next_poll,
    observe,
    stale_after,
    upload_instant,
)

NOW = datetime(2026, 10, 6, 13, 23, 44, tzinfo=UTC)
MIN = timedelta(minutes=1)
SEC = timedelta(seconds=1)


def test_upload_instant_reads_v2_utc() -> None:
    """v2 sends utcDateTime in real UTC."""
    assert upload_instant({"utcDateTime": "2026-10-06T13:23:04Z"}) == datetime(
        2026, 10, 6, 13, 23, 4, tzinfo=UTC
    )


def test_upload_time_without_zone_is_utc() -> None:
    """A stamp without a time zone is taken as UTC, like the documented ones."""
    assert upload_instant({"utcDateTime": "2026-10-06T13:23:04"}) == upload_instant(
        {"utcDateTime": "2026-10-06T13:23:04Z"}
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("not a time", None),
        (42, None),
        ("0001-01-01T00:00:00+05:00", None),
    ],
)
def test_upload_instant_rejects_unusable_values(value: object, expected: None) -> None:
    """Anything that is not a usable time gives None."""
    assert upload_instant({"utcDateTime": value}) is expected


def test_stale_after_grows_with_slow_uploaders() -> None:
    """At least 20 minutes, and always room for one missed upload."""
    assert stale_after(timedelta(minutes=5)) == timedelta(minutes=20)
    assert stale_after(timedelta(minutes=15)) == timedelta(minutes=35)


def test_first_upload_within_tolerance_counts_as_just_seen() -> None:
    """Clocks may differ: an upload that looks up to an hour old is trusted."""
    tracker, seen = observe(UploadTracker.start(NOW), NOW - 50 * MIN, NOW)

    assert seen is Seen.NEW
    assert not is_stale(tracker, NOW + 19 * MIN)
    assert is_stale(tracker, NOW + 21 * MIN)


def test_first_upload_far_in_the_past_is_stale_at_once() -> None:
    """After a restart, an upload hours old is not shown as current."""
    old = NOW - STARTUP_TOLERANCE - MIN
    tracker, _ = observe(UploadTracker.start(NOW), old, NOW)

    assert is_stale(tracker, NOW)


def test_regression_staleness_ignores_a_constant_clock_offset() -> None:
    """Regression: staleness compared the cloud's stamp with our clock.

    A cloud stamp 30 minutes behind our clock (plant time zone set wrong, or
    our clock fast) made every live sensor permanently unavailable although
    uploads kept arriving.
    """
    tracker = UploadTracker.start(NOW)
    behind = timedelta(minutes=30)
    for i in range(12):
        now = NOW + 5 * MIN * i
        tracker, seen = observe(tracker, now - behind, now)
        assert seen is Seen.NEW
        assert not is_stale(tracker, now + MIN)


@pytest.mark.parametrize(
    ("stamp", "expected"),
    [
        (NOW - 10 * MIN, Seen.SAME),
        (NOW - 15 * MIN, Seen.OLDER),
        (NOW + 2 * MAX_FUTURE, Seen.BOGUS),
        (None, Seen.NONE),
    ],
)
def test_only_newer_uploads_count_as_new(
    stamp: datetime | None, expected: Seen
) -> None:
    """Repeats, older and absurdly future uploads leave the tracker alone."""
    tracker, _ = observe(UploadTracker.start(NOW), NOW - 10 * MIN, NOW)

    after, seen = observe(tracker, stamp, NOW + MIN)

    assert seen is expected
    assert after.newest == tracker.newest
    assert after.seen_at == tracker.seen_at


def test_regression_future_stamp_does_not_freeze_the_tracker() -> None:
    """Regression: one far-future stamp became the newest upload for good."""
    tracker, _ = observe(UploadTracker.start(NOW), NOW, NOW)
    tracker, _ = observe(tracker, datetime(9999, 12, 31, tzinfo=UTC), NOW + MIN)

    tracker, seen = observe(tracker, NOW + 5 * MIN, NOW + 6 * MIN)

    assert seen is Seen.NEW


def _learn(cadence: timedelta, uploads: int = 3) -> UploadTracker:
    """Poll every LEARNING_POLL while a dongle uploads every `cadence`."""
    tracker = UploadTracker.start(NOW)
    now = NOW
    while now < NOW + cadence * uploads:
        latest = NOW + cadence * ((now - NOW) // cadence)
        tracker, _ = observe(tracker, latest, now)
        now += LEARNING_POLL
    return tracker


@pytest.mark.parametrize("minutes", [1, 2, 5, 10, 15])
def test_cadence_is_learned_from_closely_watched_uploads(minutes: int) -> None:
    """Regression: only cadences slower than 5 minutes could ever be learned."""
    tracker = _learn(timedelta(minutes=minutes))

    assert tracker.confirmed
    assert tracker.cadence == timedelta(minutes=minutes)


def test_regression_one_missed_upload_does_not_change_the_cadence() -> None:
    """Regression: a 10-minute gap from one missed upload became the cadence."""
    tracker = _learn(5 * MIN)
    last = tracker.newest
    assert last is not None
    # The upload at +5 never arrives; overdue polls see +10 next.
    now = last + 10 * MIN - OVERDUE_POLL / 2
    tracker, _ = observe(tracker, last, now)
    tracker, seen = observe(tracker, last + 10 * MIN, now + OVERDUE_POLL)

    assert seen is Seen.NEW
    assert tracker.cadence == 5 * MIN


def _closely_watched_gaps(
    tracker: UploadTracker, gaps_minutes: list[int]
) -> UploadTracker:
    """Poll every 30 s while uploads arrive at these gaps after the newest."""
    stamp = tracker.newest
    assert stamp is not None
    now = stamp + 30 * SEC
    for gap in gaps_minutes:
        while now < stamp + gap * MIN:
            tracker, _ = observe(tracker, stamp, now)
            now += 30 * SEC
        stamp += gap * MIN
        tracker, _ = observe(tracker, stamp, now)
    return tracker


@pytest.mark.parametrize(
    ("gaps", "expected"),
    [
        # Not a multiple of 5: two consistent gaps are enough.
        ([7, 7], 7),
        # A multiple of 5 looks like missed uploads: three are needed.
        ([10, 10], 5),
        ([10, 10, 10], 10),
        # A smaller gap in between disproves the suspected change.
        ([10, 5, 10, 10], 5),
    ],
)
def test_a_real_cadence_change_needs_consistent_gaps(
    gaps: list[int], expected: int
) -> None:
    """The cadence only changes on consistent, closely watched evidence."""
    tracker = _closely_watched_gaps(_learn(5 * MIN), gaps)

    assert tracker.cadence == expected * MIN


def test_late_polls_do_not_teach_a_slower_cadence() -> None:
    """When our polls run late (a busy Home Assistant), the gaps they see are
    multiples of the real cadence; only closely watched gaps may teach it."""
    tracker = _learn(MIN)
    last = tracker.newest
    assert last is not None and tracker.last_poll is not None
    now = tracker.last_poll
    for _ in range(6):
        now += 2 * MIN
        tracker, _ = observe(tracker, last + MIN * int((now - last) / MIN), now)

    assert tracker.cadence == MIN


def test_next_poll_while_learning_and_without_any_upload_time() -> None:
    """Learning polls closely; no usable time at all backs off after a while."""
    learning, _ = observe(UploadTracker.start(NOW), NOW - 10 * SEC, NOW)
    blind = UploadTracker.start(NOW)

    assert next_poll(learning, NOW) == LEARNING_POLL
    assert next_poll(blind, NOW + MIN) == OVERDUE_POLL
    assert next_poll(blind, NOW + 21 * MIN) == OFFLINE_POLL


# --- Simulation: a dongle with a known rhythm, delay and clock offset -------


@dataclass(frozen=True)
class Dongle:
    """Ground truth the tracker must discover."""

    cadence: timedelta
    delay: timedelta  # upload -> visible in the cloud
    clock_offset: timedelta  # our clock minus the cloud's clock
    missed: frozenset[int]  # upload numbers that never arrive
    stops_after: int  # no uploads from this number on
    clock_step: timedelta = timedelta(0)  # our clock jumps by this ...
    step_at: int = 20  # ... at this upload (e.g. an NTP correction)

    def visible(self, cloud_now: datetime) -> datetime | None:
        """Cloud stamp of the newest upload visible at cloud time `cloud_now`."""
        n = int((cloud_now - self.delay - NOW) / self.cadence)
        n = min(n, self.stops_after - 1)
        while n >= 0 and n in self.missed:
            n -= 1
        return NOW + self.cadence * n if n >= 0 else None


dongles = st.builds(
    Dongle,
    cadence=st.integers(1, 15).map(lambda m: timedelta(minutes=m)),
    delay=st.integers(0, 60).map(lambda s: timedelta(seconds=s)),
    clock_offset=st.integers(-3 * 3600, 3 * 3600).map(lambda s: timedelta(seconds=s)),
    # At least 4 uploads apart, never in the first few (learning needs a start).
    missed=st.sets(st.integers(2, 18).map(lambda n: 4 * n), max_size=4).map(frozenset),
    stops_after=st.integers(40, 80),
    clock_step=st.integers(-300, 300).map(lambda s: timedelta(seconds=s)),
    step_at=st.integers(10, 30),
)


@settings(max_examples=300, deadline=None)
@given(dongle=dongles)
def test_property_tracker_follows_any_dongle(dongle: Dongle) -> None:
    """Whatever the rhythm, delay and clock offset, after learning:

    - each upload is seen within two minutes of becoming visible,
    - there is about one request per upload (more while learning, around a
      missed upload, and while re-settling after our clock jumps),
    - data is never stale while uploads keep coming,
    - and it is stale soon after the dongle stops.
    """
    tracker = UploadTracker.start(NOW + dongle.clock_offset)
    cloud_now = NOW
    step_time = NOW + dongle.cadence * dongle.step_at
    first_seen: dict[datetime, datetime] = {}
    polls_after_learning = 0
    while cloud_now < NOW + dongle.cadence * (dongle.stops_after + 10):
        our_now = cloud_now + dongle.clock_offset
        if cloud_now >= step_time:
            our_now += dongle.clock_step
        stamp = dongle.visible(cloud_now)
        tracker, seen = observe(tracker, stamp, our_now)
        if seen is Seen.NEW and stamp is not None:
            first_seen[stamp] = cloud_now
        stopped = cloud_now > NOW + dongle.cadence * dongle.stops_after + dongle.delay
        if tracker.confirmed and not stopped:
            polls_after_learning += 1
            assert not is_stale(tracker, our_now), "stale while uploading"
        if cloud_now > (
            NOW
            + dongle.cadence * dongle.stops_after
            + dongle.delay
            + stale_after(dongle.cadence)
            + OFFLINE_POLL
            + MIN
        ):
            assert is_stale(tracker, our_now), "not stale after the dongle stopped"
        interval = next_poll(tracker, our_now)
        assert interval >= MIN_POLL
        cloud_now += interval

    assert tracker.confirmed
    assert tracker.cadence == dongle.cadence
    # Latency, once learned (the first few uploads are spent learning).
    # Excluding the uploads right after a clock jump, while it re-settles.
    settling = (step_time, step_time + dongle.cadence * 8)
    late = {
        stamp: seen_at - (stamp + dongle.delay)
        for stamp, seen_at in first_seen.items()
        if stamp > NOW + dongle.cadence * 4
        and (stamp - dongle.cadence) in first_seen
        and not settling[0] <= stamp <= settling[1]
    }
    assert max(late.values(), default=timedelta(0)) <= 2 * MIN
    uploads_after_learning = len(
        [s for s in first_seen if s > NOW + dongle.cadence * 4]
    )
    # One poll per upload; around a missed upload, overdue polls until the
    # next one arrives.
    per_miss = int(dongle.cadence / OVERDUE_POLL) + 2
    # A forward clock jump makes polls early until re-settled.
    settle = per_miss * 8 if dongle.clock_step > timedelta(0) else 0
    assert (
        polls_after_learning
        <= uploads_after_learning + per_miss * len(dongle.missed) + settle + 10
    )


@settings(max_examples=300, deadline=None)
@given(
    since=st.integers(0, 7 * 24 * 3600).map(lambda s: timedelta(seconds=s)),
    minutes=st.integers(1, 15),
)
def test_property_poll_interval_is_bounded(since: timedelta, minutes: int) -> None:
    """Never a tight loop, never longer than a cadence plus margin."""
    tracker = _learn(timedelta(minutes=minutes))
    assert tracker.last_poll is not None

    interval = next_poll(tracker, tracker.last_poll + since)

    assert (
        MIN_POLL
        <= interval
        <= max(timedelta(minutes=minutes) + timedelta(minutes=2), OFFLINE_POLL)
    )


def test_default_cadence_is_five_minutes() -> None:
    """Known installs upload every 5 minutes."""
    assert DEFAULT_CADENCE == 5 * MIN
