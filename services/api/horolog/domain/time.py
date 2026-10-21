"""Time discretisation.

The entire solver works in integer *slots* measured from a horizon origin.
Nothing inside `horolog.solver` ever sees a datetime; conversion happens only at
the edges. This keeps the CP model integral (CP-SAT requires it) and makes the
whole engine trivially testable without timezones.
"""

from __future__ import annotations

from datetime import datetime, timedelta

SLOT_MINUTES = 15
SLOTS_PER_HOUR = 60 // SLOT_MINUTES
SLOTS_PER_DAY = 24 * SLOTS_PER_HOUR


def minutes_to_slots(minutes: int) -> int:
    """Round a duration up to whole slots.

    Rounding *up* is deliberate: under-allocating a 20-minute task to one slot
    would silently schedule 15 minutes of work and report it complete.
    """
    if minutes < 0:
        raise ValueError(f"duration must be non-negative, got {minutes}")
    return -(-minutes // SLOT_MINUTES)


def slots_to_minutes(slots: int) -> int:
    return slots * SLOT_MINUTES


def to_slot(moment: datetime, origin: datetime, *, round_up: bool = False) -> int:
    """Convert in local wall time; round starts/busy ends up when requested."""
    if moment.tzinfo is None or origin.tzinfo is None:
        raise ValueError("datetimes must be timezone-aware")
    delta = moment.astimezone(origin.tzinfo) - origin
    seconds = delta.total_seconds()
    width = SLOT_MINUTES * 60
    return int(-(-seconds // width)) if round_up else int(seconds // width)


def from_slot(slot: int, origin: datetime) -> datetime:
    return origin + timedelta(minutes=slot * SLOT_MINUTES)


def day_of(slot: int) -> int:
    """Index of the day containing `slot`, counting from the horizon origin."""
    return slot // SLOTS_PER_DAY


def day_start(day_index: int) -> int:
    return day_index * SLOTS_PER_DAY
