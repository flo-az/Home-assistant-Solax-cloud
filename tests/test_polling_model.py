"""Tests for tracking the dongle's uploads: when to poll, and when data is stale.

The cloud stamps each upload with its own clock. The tracker measures time
with a monotonic clock (`now`), which never jumps; Home Assistant's wall
clock (`wall`) is only used to judge how old the first upload is and to
reject absurd future stamps.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import random

from hypothesis import given, settings, strategies as st
import pytest

from custom_components.solax_cloud.polling import (
    DEFAULT_CADENCE,
    LEARNING_LIMIT,
    LEARNING_POLL,
    MAX_FUTURE,
    MIN_POLL,
    OFFLINE_POLL,
    OVERDUE_POLL,
    POLL_AFTER_UPLOAD,
    PROBE_EVERY,
    STARTUP_TOLERANCE,
    Seen,
    UploadTracker,
    is_stale,
    next_poll,
    observe,
    stale_after,
    tolerance,
    upload_instant,
)

NOW = datetime(2026, 10, 6, 13, 23, 44, tzinfo=UTC)
MIN = timedelta(minutes=1)
SEC = timedelta(seconds=1)


def see(
    tracker: UploadTracker, stamp: datetime | None, now: datetime
) -> tuple[UploadTracker, Seen]:
    """A poll where the wall clock agrees with the monotonic clock."""
    return observe(tracker, stamp, now, now)


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


@pytest.mark.parametrize("value", [None, "not a time", 42, "0001-01-01T00:00:00+05:00"])
def test_upload_instant_rejects_unusable_values(value: object) -> None:
    """Anything that is not a usable time gives None."""
    assert upload_instant({"utcDateTime": value}) is None


def test_stale_after_grows_with_slow_uploaders() -> None:
    """At least 20 minutes, and always room for one missed upload."""
    assert stale_after(5 * MIN) == 20 * MIN
    assert stale_after(15 * MIN) == 35 * MIN


@pytest.mark.parametrize(
    ("age", "stale"),
    [(STARTUP_TOLERANCE - MIN, False), (STARTUP_TOLERANCE + MIN, True)],
    ids=["within-tolerance", "beyond-tolerance"],
)
def test_first_upload_age_is_judged_by_the_wall_clock(
    age: timedelta, stale: bool
) -> None:
    """Clocks may differ by hours: only an upload older than that is stale at once."""
    tracker, seen = see(UploadTracker.start(NOW), NOW - age, NOW)

    assert seen is Seen.NEW
    assert is_stale(tracker, NOW) is stale


def test_regression_staleness_ignores_a_constant_clock_offset() -> None:
    """Regression: staleness compared the cloud's stamp with our clock."""
    tracker = UploadTracker.start(NOW)
    for i in range(12):
        now = NOW + 5 * MIN * i
        tracker, seen = see(tracker, now - 30 * MIN, now)
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
    tracker, _ = see(UploadTracker.start(NOW), NOW - 10 * MIN, NOW)

    after, seen = see(tracker, stamp, NOW + MIN)

    assert seen is expected
    assert after.newest == tracker.newest
    assert after.seen_at == tracker.seen_at


def test_regression_future_stamp_does_not_freeze_the_tracker() -> None:
    """Regression: one far-future stamp became the newest upload for good."""
    tracker, _ = see(UploadTracker.start(NOW), NOW, NOW)
    tracker, _ = see(tracker, datetime(9999, 12, 31, tzinfo=UTC), NOW + MIN)

    tracker, seen = see(tracker, NOW + 5 * MIN, NOW + 6 * MIN)

    assert seen is Seen.NEW


def test_regression_cloud_clock_correction_is_adopted() -> None:
    """Regression: one stamp hours ahead made every later upload "older".

    Three consecutive, advancing older stamps mean the cloud's clock was
    corrected; they are adopted instead of being ignored for up to a day.
    """
    tracker, _ = see(UploadTracker.start(NOW), NOW, NOW)
    tracker, _ = see(tracker, NOW + 12 * 60 * MIN, NOW + MIN)  # 12 h ahead
    seen_list = []
    for i in range(1, 4):
        tracker, seen = see(tracker, NOW + 5 * MIN * i, NOW + 5 * MIN * i + MIN)
        seen_list.append(seen)

    assert seen_list == [Seen.OLDER, Seen.OLDER, Seen.RESET]
    assert tracker.newest == NOW + 15 * MIN
    assert not is_stale(tracker, NOW + 16 * MIN)


def _learn(cadence: timedelta, uploads: int = 3) -> UploadTracker:
    """Poll every LEARNING_POLL while a dongle uploads every `cadence`."""
    tracker = UploadTracker.start(NOW)
    now = NOW
    while now < NOW + cadence * uploads:
        tracker, _ = see(tracker, NOW + cadence * ((now - NOW) // cadence), now)
        now += LEARNING_POLL
    return tracker


@pytest.mark.parametrize("minutes", [1, 2, 5, 10, 15])
def test_cadence_is_learned_from_closely_watched_uploads(minutes: int) -> None:
    """Regression: only cadences slower than 5 minutes could ever be learned."""
    tracker = _learn(timedelta(minutes=minutes))

    assert tracker.confirmed
    assert tracker.cadence == timedelta(minutes=minutes)


def test_learning_deadline_is_not_fooled_by_missed_uploads() -> None:
    """With every third upload missing, half the gaps are double: the
    deadline estimate must come from the short gaps, not the median."""
    stamps = [NOW + 3 * MIN * n for n in range(40) if n % 3 != 0 or n == 0]
    tracker = UploadTracker.start(NOW)
    now = NOW
    while now < NOW + LEARNING_LIMIT + 10 * MIN:
        visible = [s for s in stamps if s <= now]
        jitter = timedelta(seconds=(len(visible) * 7) % 13 - 6)
        tracker, _ = see(tracker, visible[-1] + jitter if visible else None, now)
        now += LEARNING_POLL

    assert abs(tracker.cadence - 3 * MIN) <= 15 * SEC


def test_learning_ends_even_with_jittery_uploads() -> None:
    """Uploads jittering by ±20 s may never give two matching gaps: after
    LEARNING_LIMIT the median gap is taken instead of learning forever."""
    rng = random.Random(1)
    stamps = [NOW + 5 * MIN * n + rng.uniform(-20, 20) * SEC for n in range(30)]
    tracker = UploadTracker.start(NOW)
    now = NOW
    while now < NOW + LEARNING_LIMIT + 10 * MIN:
        visible = [s for s in stamps if s <= now]
        tracker, _ = see(tracker, visible[-1] if visible else None, now)
        now += LEARNING_POLL

    assert tracker.confirmed
    assert abs(tracker.cadence - 5 * MIN) <= 30 * SEC


def test_regression_one_missed_upload_does_not_change_the_cadence() -> None:
    """Regression: a 10-minute gap from one missed upload became the cadence."""
    tracker = _learn(5 * MIN)
    last = tracker.newest
    assert last is not None
    now = last + 10 * MIN - OVERDUE_POLL / 2
    tracker, _ = see(tracker, last, now)
    tracker, seen = see(tracker, last + 10 * MIN, now + OVERDUE_POLL)

    assert seen is Seen.NEW
    assert tracker.cadence == 5 * MIN


def _run(
    tracker: UploadTracker,
    start: datetime,
    duration: timedelta,
    stamp_at: Callable[[datetime], datetime | None],
) -> UploadTracker:
    """Follow the real scheduler; stamp_at(now) is the newest visible stamp."""
    now = start
    while now < start + duration:
        tracker, _ = see(tracker, stamp_at(now), now)
        now += next_poll(tracker, now)
    return tracker


@pytest.mark.parametrize(
    ("minutes", "within"),
    [
        # Gaps of 4 or 6 minutes at the old schedule give it away at once.
        (2, timedelta(hours=1)),
        (3, timedelta(hours=1)),
        # 1 divides 5: the old schedule still sees 5-minute gaps; only the
        # periodic close look can notice.
        (1, PROBE_EVERY + timedelta(hours=1)),
    ],
)
def test_regression_switch_to_a_faster_rhythm_is_noticed(
    minutes: int, within: timedelta
) -> None:
    """Regression: after confirming 5 minutes, a dongle switching to a faster
    rhythm was polled every 5 minutes forever, missing most uploads."""
    tracker = _learn(5 * MIN)
    assert tracker.last_poll is not None
    switch, faster = tracker.last_poll, timedelta(minutes=minutes)

    tracker = _run(
        tracker,
        switch,
        within,
        lambda now: switch + faster * ((now - switch) // faster),
    )

    assert tracker.cadence == faster


def test_periodic_probe_corrects_a_mislearned_cadence() -> None:
    """Alternating missed uploads while learning can teach 2x the cadence;
    the periodic close look corrects it within PROBE_EVERY."""
    tracker = _learn(10 * MIN)  # really a 5-minute dongle that missed every other
    assert tracker.last_poll is not None
    start = tracker.last_poll

    tracker = _run(
        tracker,
        start,
        PROBE_EVERY + 30 * MIN,
        lambda now: start + 5 * MIN * ((now - start) // (5 * MIN)),
    )

    assert tracker.cadence == 5 * MIN


def test_regression_wall_clock_jump_does_not_derail_polling() -> None:
    """Regression: our clock jumping back 3 h scheduled the next poll hours
    away and kept stale data "current" meanwhile. The tracker runs on a
    monotonic clock; the wall clock jumping changes nothing."""
    tracker = _learn(5 * MIN)
    assert tracker.last_poll is not None and tracker.newest is not None
    now, newest = tracker.last_poll, tracker.newest

    for _ in range(10):
        now += 30 * SEC
        tracker, _ = observe(tracker, newest, now, now - timedelta(hours=3))
        assert next_poll(tracker, now) <= 5 * MIN + POLL_AFTER_UPLOAD
    assert not is_stale(tracker, now)
    assert is_stale(tracker, now + 25 * MIN)


def test_learning_backs_off_when_no_upload_arrives() -> None:
    """A dongle silent right after start is not polled every 30 s for half an
    hour: after one maximal cadence without a new upload, every 5 minutes."""
    tracker, _ = see(UploadTracker.start(NOW), NOW - 10 * SEC, NOW)

    assert next_poll(tracker, NOW + 10 * MIN) == LEARNING_POLL
    assert next_poll(tracker, NOW + 16 * MIN) == OFFLINE_POLL


def _closely_watched_gaps(
    tracker: UploadTracker, gaps_minutes: list[int]
) -> UploadTracker:
    """Poll every 30 s while uploads arrive at these gaps after the newest."""
    stamp = tracker.newest
    assert stamp is not None and tracker.last_poll is not None
    now = tracker.last_poll + 30 * SEC
    for gap in gaps_minutes:
        while now < stamp + gap * MIN:
            tracker, _ = see(tracker, stamp, now)
            now += 30 * SEC
        stamp += gap * MIN
        tracker, _ = see(tracker, stamp, now)
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
def test_a_cadence_change_needs_consistent_gaps(gaps: list[int], expected: int) -> None:
    """The cadence only changes on consistent, closely watched evidence."""
    tracker = _closely_watched_gaps(_learn(5 * MIN), gaps)

    assert tracker.cadence == expected * MIN


def test_a_poll_before_the_previous_one_is_not_close() -> None:
    """Only polls up to TRUSTED_POLL_GAP *after* the previous one count as close."""
    tracker = _learn(5 * MIN)
    assert tracker.last_poll is not None and tracker.newest is not None

    tracker, _ = see(tracker, tracker.newest + 10 * MIN, tracker.last_poll - 5 * MIN)

    assert tracker.pending_gap is None


def test_while_learning_staleness_allows_the_slowest_rhythm() -> None:
    """Until the rhythm is known, a 15-minute dongle missing one upload is
    not stale yet."""
    tracker, _ = see(UploadTracker.start(NOW), NOW, NOW)

    assert not is_stale(tracker, NOW + 34 * MIN)
    assert is_stale(tracker, NOW + 36 * MIN)


def test_a_quiet_dongle_is_polled_every_5_minutes() -> None:
    """Once stale, polling backs off."""
    tracker = _learn(5 * MIN)
    assert tracker.seen_at is not None

    assert next_poll(tracker, tracker.seen_at + 21 * MIN) == OFFLINE_POLL


def _deadline_tracker(gaps_seconds: list[int]) -> UploadTracker:
    """A tracker still learning at the deadline with these measured gaps."""
    return UploadTracker(
        started=NOW,
        newest=NOW,
        seen_at=NOW,
        gaps=tuple(timedelta(seconds=g) for g in gaps_seconds),
        first_gap_at=NOW,
        pending_gap=timedelta(seconds=gaps_seconds[-1]),
        pending_count=1,
        offsets=(timedelta(0),),
        last_poll=NOW,
    )


@pytest.mark.parametrize(
    ("gaps", "expected"),
    [
        # Every third upload missing: half the gaps are double.
        ([366, 174, 366, 178, 362], 176),
        ([185, 174, 190, 170, 181], 181),
    ],
)
def test_learning_deadline_takes_the_typical_short_gap(
    gaps: list[int], expected: int
) -> None:
    """At the deadline, the median of the gaps near the shortest one."""
    tracker = _deadline_tracker(gaps)

    after, _ = see(tracker, NOW, NOW + LEARNING_LIMIT)

    assert after.confirmed
    assert after.cadence == timedelta(seconds=expected)


def test_learning_continues_before_the_deadline() -> None:
    """Before LEARNING_LIMIT, unmatched gaps leave the rhythm unconfirmed."""
    tracker = _deadline_tracker([185, 174])

    after, _ = see(tracker, NOW, NOW + LEARNING_LIMIT - SEC)

    assert not after.confirmed


def _learned_at(cadence: timedelta) -> tuple[UploadTracker, datetime]:
    tracker = _learn(cadence)
    assert tracker.last_poll is not None
    return tracker, tracker.last_poll


@settings(max_examples=200, deadline=None)
@given(
    minutes=st.integers(1, 15),
    ahead=st.integers(1, 23).map(lambda h: timedelta(hours=h)),
)
def test_regression_bogus_stamp_is_overcome_under_the_real_scheduler(
    minutes: int, ahead: timedelta
) -> None:
    """Regression: after one stamp hours ahead, the real scheduler sees each
    later (older-looking) upload several times; that restarted the count of
    advancing older stamps, so slow dongles stayed stuck for up to a day."""
    cadence = timedelta(minutes=minutes)
    tracker, start = _learned_at(cadence)
    tracker, _ = see(tracker, start + ahead, start)
    now, reset_at = start, None
    while now < start + timedelta(hours=24) and reset_at is None:
        now += next_poll(tracker, now)
        tracker, seen = see(tracker, NOW + cadence * ((now - NOW) // cadence), now)
        if seen is Seen.RESET:
            reset_at = now

    assert reset_at is not None
    assert reset_at - start <= 3 * cadence + OFFLINE_POLL + MIN


def test_regression_learning_deadline_counts_from_the_first_gap() -> None:
    """Regression: after a quiet night the deadline had long passed, so the
    first measured gap (here double, from a missed upload) was confirmed."""
    morning = NOW + timedelta(hours=10)
    tracker, _ = see(UploadTracker.start(NOW), morning - 10 * MIN, morning)
    tracker, _ = see(tracker, morning - 10 * MIN, morning + 30 * SEC)
    tracker, _ = see(tracker, morning, morning + 60 * SEC)

    assert not tracker.confirmed
    tracker = _closely_watched_gaps(tracker, [5, 5])
    assert tracker.confirmed
    assert tracker.cadence == 5 * MIN


def test_next_poll_while_learning_and_without_any_upload_time() -> None:
    """Learning polls closely; no usable time at all backs off after a while."""
    learning, _ = see(UploadTracker.start(NOW), NOW - 10 * SEC, NOW)
    blind = UploadTracker.start(NOW)

    assert next_poll(learning, NOW) == LEARNING_POLL
    assert next_poll(blind, NOW + MIN) == OVERDUE_POLL
    assert next_poll(blind, NOW + 21 * MIN) == OFFLINE_POLL


# --- Simulation: a dongle with a known rhythm, delays and clocks ------------


def _mix(n: int, salt: int, modulo: int) -> int:
    """Deterministic pseudo-random number in [0, modulo) per upload."""
    return ((n * 2654435761 + salt * 40503) % 2**32) % modulo


@dataclass(frozen=True)
class Dongle:
    """Ground truth the tracker must discover."""

    cadence: timedelta
    base_delay: timedelta  # upload -> visible in the cloud ...
    delay_spread: int  # ... plus 0..this many seconds, per upload
    jitter: int  # stamps deviate from the grid by up to ± this many seconds
    wall_offset: timedelta  # our wall clock minus the cloud's clock ...
    wall_jump: timedelta  # ... which jumps by this ...
    jump_at: int  # ... at this upload
    missed: frozenset[int]  # upload numbers that never arrive
    stops_after: int  # no uploads from this number on

    def stamp(self, n: int) -> datetime:
        wobble = _mix(n, 1, 2 * self.jitter + 1) - self.jitter
        return NOW + self.cadence * n + wobble * SEC

    def visible_at(self, n: int) -> datetime:
        return self.stamp(n) + self.base_delay + _mix(n, 2, self.delay_spread + 1) * SEC

    def visible(self, true_now: datetime) -> int | None:
        """Number of the newest upload visible at true time `true_now`."""
        n = min((true_now - NOW) // self.cadence + 1, self.stops_after - 1)
        while n >= 0 and (n in self.missed or self.visible_at(n) > true_now):
            n -= 1
        return n if n >= 0 else None


dongles = st.builds(
    Dongle,
    cadence=st.integers(1, 15).map(lambda m: timedelta(minutes=m)),
    base_delay=st.integers(0, 40).map(lambda s: timedelta(seconds=s)),
    delay_spread=st.integers(0, 20),
    jitter=st.integers(0, 5),
    wall_offset=st.integers(-150, 150).map(lambda m: timedelta(minutes=m)),
    wall_jump=st.integers(-180, 180).map(lambda m: timedelta(minutes=m)),
    jump_at=st.integers(5, 30),
    # At least 3 apart (alternating misses can mislead learning; the
    # periodic probe for that is tested separately), from upload 3 on.
    missed=st.sets(st.integers(1, 20).map(lambda n: 3 * n), max_size=5).map(frozenset),
    stops_after=st.integers(40, 80),
)


@settings(max_examples=300, deadline=None)
@given(dongle=dongles)
def test_property_tracker_follows_any_dongle(dongle: Dongle) -> None:
    """Whatever the rhythm, delays, jitter and clocks:

    - after learning, (almost) every upload is seen, within about 100 s of
      becoming visible;
    - about one request per upload;
    - no poll is scheduled further away than one cadence plus margin;
    - data is never stale while uploads keep coming, and is stale soon
      after the dongle stops.
    """
    tracker = UploadTracker.start(NOW)  # monotonic = true time here
    true_now = NOW
    first_seen: dict[int, datetime] = {}
    polls_after_learning = 0
    last_upload_time = dongle.visible_at(dongle.stops_after - 1)
    end = last_upload_time + stale_after(dongle.cadence) + 2 * OFFLINE_POLL
    while true_now < end:
        wall = true_now + dongle.wall_offset
        if true_now >= NOW + dongle.cadence * dongle.jump_at:
            wall += dongle.wall_jump
        n = dongle.visible(true_now)
        tracker, seen = observe(
            tracker, None if n is None else dongle.stamp(n), true_now, wall
        )
        if seen is Seen.NEW and n is not None:
            first_seen.setdefault(n, true_now)
        # Why the interval cap never binds: the expected next upload is at
        # most one cadence after the newest was seen, which is in the past.
        if tracker.offsets and tracker.newest and tracker.seen_at:
            assert tracker.newest + min(tracker.offsets) <= tracker.seen_at
            assert tracker.seen_at <= true_now
        uploading = true_now <= last_upload_time
        if uploading and n is not None:
            assert not is_stale(tracker, true_now), "stale while uploading"
        if true_now > last_upload_time + stale_after(dongle.cadence) + 2 * MIN:
            assert is_stale(tracker, true_now), "not stale after the dongle stopped"
        if tracker.confirmed and uploading:
            polls_after_learning += 1
        interval = next_poll(tracker, true_now)
        assert interval >= MIN_POLL
        if not is_stale(tracker, true_now):
            assert interval <= tracker.cadence + POLL_AFTER_UPLOAD
        true_now += interval

    # With jittery stamps the learned cadence is an average of measured gaps.
    assert abs(tracker.cadence - dongle.cadence) <= tolerance(dongle.cadence)
    expected = [n for n in range(6, dongle.stops_after) if n not in dongle.missed]
    unseen = [n for n in expected if n not in first_seen]
    # An upload superseded before we looked cannot be seen; rare here.
    assert len(unseen) <= len(expected) // 20 + 1, f"uploads not seen: {unseen}"
    late = [first_seen[n] - dongle.visible_at(n) for n in expected if n in first_seen]
    assert late
    assert max(late) <= POLL_AFTER_UPLOAD + OVERDUE_POLL + 25 * SEC
    per_miss = int(dongle.cadence / OVERDUE_POLL) + 2
    probes = int(dongle.cadence * dongle.stops_after / PROBE_EVERY) + 1
    budget = (
        len(expected)
        + per_miss * (len(dongle.missed) + probes)
        + (len(expected) // 4 if dongle.jitter or dongle.delay_spread else 0)
        + 10
    )
    assert polls_after_learning <= budget


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
        <= max(timedelta(minutes=minutes) + POLL_AFTER_UPLOAD, OFFLINE_POLL)
    )


def test_default_cadence_is_five_minutes() -> None:
    """Known installs upload every 5 minutes."""
    assert DEFAULT_CADENCE == 5 * MIN
