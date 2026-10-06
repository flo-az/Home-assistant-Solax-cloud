"""Add up power readings, one per Solax upload, into energy.

Power is assumed to change linearly between two consecutive uploads. Each
interval contributes the area under that line, split into the part above
zero (e.g. battery charging) and below zero (discharging).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# Uploads further apart than this are not bridged (Home Assistant or the
# cloud was down; the power in between is unknown).
MAX_UPLOAD_GAP = timedelta(minutes=15)

# An upload stamped further ahead of our clock than this is bogus. Taking it
# as the series' last upload would make every real upload look older.
MAX_CLOCK_SKEW = timedelta(hours=1)


@dataclass(frozen=True)
class EnergySeries:
    """The last upload added up so far and its power reading in W."""

    last_upload: datetime | None
    last_power: float | None


EMPTY = EnergySeries(None, None)


def restored_series(
    last_upload: datetime | None, last_power: float | None, now: datetime
) -> EnergySeries:
    """A stored series, if complete and not from the future; else a new one."""
    if last_upload is None or last_power is None or last_upload > now + MAX_CLOCK_SKEW:
        return EMPTY
    return EnergySeries(last_upload, last_power)


def _area_kwh(start_w: float, end_w: float, hours: float) -> tuple[float, float]:
    """Area above and below zero under the line from start_w to end_w."""
    if start_w >= 0 and end_w >= 0:
        return (start_w + end_w) / 2 * hours / 1000, 0.0
    if start_w <= 0 and end_w <= 0:
        return 0.0, -(start_w + end_w) / 2 * hours / 1000
    # The line crosses zero: one triangle on each side, in proportion to the
    # readings' magnitudes.
    above, below = max(start_w, end_w), -min(start_w, end_w)
    span = above + below
    return (
        above * above / (2 * span) * hours / 1000,
        below * below / (2 * span) * hours / 1000,
    )


def step(
    series: EnergySeries, upload: datetime, power: float | None, now: datetime
) -> tuple[EnergySeries, float, float]:
    """Add one upload; return the new series and kWh added above/below zero.

    Repeated, older and future-stamped uploads leave the series unchanged.
    An interval longer than MAX_UPLOAD_GAP or with a missing reading adds
    nothing, but the series continues from this upload.
    """
    if upload > now + MAX_CLOCK_SKEW:
        return series, 0.0, 0.0
    if series.last_upload is not None and upload <= series.last_upload:
        return series, 0.0, 0.0

    added = (0.0, 0.0)
    if (
        power is not None
        and series.last_power is not None
        and series.last_upload is not None
        and upload - series.last_upload <= MAX_UPLOAD_GAP
    ):
        hours = (upload - series.last_upload).total_seconds() / 3600
        added = _area_kwh(series.last_power, power, hours)
    return EnergySeries(upload, power), *added
