"""Tests for adding up per-upload power readings into energy (no Home Assistant).

Power is assumed to change linearly between two uploads. Expected values are
hand-computed; 3000 W for 5 minutes is 3000 * 5 / 60 / 1000 = 0.25 kWh.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given, settings, strategies as st
import pytest

from custom_components.solax_cloud.energy import (
    MAX_CLOCK_SKEW,
    MAX_UPLOAD_GAP,
    EnergySeries,
    restored_series,
    step,
)

T0 = datetime(2026, 10, 6, 13, 0, 0, tzinfo=UTC)
NOW = T0 + timedelta(days=1)
START = EnergySeries(last_upload=None, last_power=None)


def run(readings: list[tuple[float, float | None]], now: datetime = NOW):
    """Feed (minutes after T0, power) readings; return (positive, negative) kWh."""
    series, positive, negative = START, 0.0, 0.0
    for minutes, power in readings:
        series, pos, neg = step(series, T0 + timedelta(minutes=minutes), power, now)
        positive += pos
        negative += neg
    return positive, negative


@pytest.mark.parametrize(
    ("readings", "expected"),
    [
        pytest.param([(0, 3000), (5, 3000)], (0.25, 0.0), id="constant-positive"),
        pytest.param([(0, -1200), (5, -1200)], (0.0, 0.1), id="constant-negative"),
        pytest.param([(0, 1000), (5, 3000)], (2000 * 5 / 60 / 1000, 0.0), id="ramp"),
        # Crosses zero after 3/4 of the 5 minutes: two triangles,
        # 3000 W * 3.75 min / 2 and 1000 W * 1.25 min / 2.
        pytest.param(
            [(0, 3000), (5, -1000)],
            (3000 * 3.75 / 2 / 60 / 1000, 1000 * 1.25 / 2 / 60 / 1000),
            id="sign-change",
        ),
        pytest.param(
            [(0, 3000), (5, 0)], (3000 * 5 / 2 / 60 / 1000, 0.0), id="to-zero"
        ),
        pytest.param([(0, 1000), (15, 1000)], (0.25, 0.0), id="gap-of-exactly-15-min"),
        pytest.param(
            [(0, 1000), (15 + 1 / 60, 1000)], (0.0, 0.0), id="gap-over-15-min"
        ),
        pytest.param(
            [(0, 3000), (5, None), (10, 3000), (15, 3000)],
            (0.25, 0.0),
            id="missing-reading-breaks-series",
        ),
        pytest.param(
            [(0, 3000), (5, 3000), (5, 3000), (2, 3000), (10, 3000)],
            (0.5, 0.0),
            id="repeat-and-past-ignored",
        ),
    ],
)
def test_energy_between_uploads(
    readings: list[tuple[float, float | None]], expected: tuple[float, float]
) -> None:
    """Each pair of consecutive uploads adds the area under the line between them."""
    assert run(readings) == pytest.approx(expected, abs=1e-12)


def test_regression_future_upload_does_not_freeze_the_series() -> None:
    """Regression: one upload stamped in the future blocked all later ones.

    Every real upload compared as older than it and was ignored, and the
    stored series kept it that way across restarts.
    """
    now = T0 + timedelta(minutes=10)
    readings = [(0, 3000), (60 * 24 * 365 * 73, 3000), (5, 3000), (10, 3000)]

    assert run(readings, now=now) == pytest.approx((0.5, 0.0))


def test_upload_slightly_ahead_of_our_clock_counts() -> None:
    """Clocks differ; an upload up to MAX_CLOCK_SKEW ahead is still real."""
    now = T0
    ahead = MAX_CLOCK_SKEW.total_seconds() / 60

    assert run([(ahead - 5, 3000), (ahead, 3000)], now=now) == pytest.approx(
        (0.25, 0.0)
    )


@pytest.mark.parametrize(
    ("last_upload", "last_power", "usable"),
    [
        (T0, 3000.0, True),
        (NOW + timedelta(hours=2), 3000.0, False),
        (T0, None, False),
        (None, 3000.0, False),
    ],
    ids=["valid", "in-the-future", "no-power", "no-upload"],
)
def test_restored_series_is_only_trusted_when_complete(
    last_upload: datetime | None, last_power: float | None, usable: bool
) -> None:
    """A stored series continues only if it is complete and not from the future."""
    series = restored_series(last_upload, last_power, NOW)

    expected = EnergySeries(last_upload, last_power) if usable else START
    assert series == expected


# Reference model: integrate the straight line between readings numerically,
# without the closed form used by the implementation.
REFERENCE_STEPS = 2000


def _counts(previous: tuple[float, float | None] | None, minutes: float) -> bool:
    """Whether the interval since `previous` is bridged (times as datetimes)."""
    gap = (T0 + timedelta(minutes=minutes)) - (T0 + timedelta(minutes=previous[0]))
    return timedelta(0) < gap <= MAX_UPLOAD_GAP


def _is_newer(previous: tuple[float, float | None] | None, minutes: float) -> bool:
    return previous is None or T0 + timedelta(minutes=minutes) > T0 + timedelta(
        minutes=previous[0]
    )


def _reference(readings: list[tuple[float, float | None]]) -> tuple[float, float]:
    positive = negative = 0.0
    last: tuple[float, float | None] | None = None
    for minutes, power in readings:
        if not _is_newer(last, minutes):
            continue
        if (
            last is not None
            and power is not None
            and last[1] is not None
            and _counts(last, minutes)
        ):
            (t0, p0), dt = last, (minutes - last[0]) / REFERENCE_STEPS
            for i in range(REFERENCE_STEPS):
                mid = t0 + (i + 0.5) * dt
                p = p0 + (power - p0) * (mid - t0) / (minutes - t0)
                if p > 0:
                    positive += p * dt / 60 / 1000
                else:
                    negative += -p * dt / 60 / 1000
        last = (minutes, power)
    return positive, negative


readings = st.lists(
    st.tuples(
        st.floats(min_value=0, max_value=20).map(lambda m: round(m, 3)),
        st.none() | st.floats(min_value=-10_000, max_value=10_000),
    ),
    min_size=1,
    max_size=12,
).map(
    # Mostly increasing times, with repeats and steps back mixed in.
    lambda steps: [
        (sum(gap for gap, _ in steps[: i + 1]) - (5 if i % 7 == 6 else 0), power)
        for i, (_, power) in enumerate(steps)
    ]
)


@settings(max_examples=300, deadline=None)
@given(readings=readings)
def test_property_matches_numeric_integration(
    readings: list[tuple[float, float | None]],
) -> None:
    """The closed-form result equals a fine numeric integral of the same line."""
    largest = max((abs(p) for _, p in readings if p is not None), default=0)
    span = max((m for m, _ in readings), default=0)
    # Midpoint rule error per interval is at most a few W*min.
    tolerance = largest * span / 60 / 1000 / REFERENCE_STEPS * 4 + 1e-9

    expected = _reference(readings)
    assert run(readings) == pytest.approx(expected, abs=tolerance)


@settings(max_examples=300, deadline=None)
@given(readings=readings)
def test_property_energy_is_never_negative_and_net_is_exact(
    readings: list[tuple[float, float | None]],
) -> None:
    """Both directions only add, and their difference is the signed trapezoid."""
    positive, negative = run(readings)

    signed = 0.0
    last = None
    for minutes, power in readings:
        if not _is_newer(last, minutes):
            continue
        if (
            last is not None
            and power is not None
            and last[1] is not None
            and _counts(last, minutes)
        ):
            signed += (last[1] + power) / 2 * (minutes - last[0]) / 60 / 1000
        last = (minutes, power)
    assert positive >= 0 and negative >= 0
    assert positive - negative == pytest.approx(signed, abs=1e-9)
