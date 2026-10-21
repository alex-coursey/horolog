"""The single scheduling primitive.

Focus Time, Habits, Tasks, Buffers and Smart Meetings are one activity type under
five names (see plan §1.2 — SelfPlanner models exactly this as a COP). `kind` is
carried for presentation and for tie-breaking only; the solver reads the numeric
fields and nothing else. Adding a sixth feature means adding a `kind` and a
factory, never a new solver path.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from enum import IntEnum, StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator

from horolog.domain.time import SLOT_MINUTES, from_slot, minutes_to_slots, to_slot


class Priority(IntEnum):
    """P1 (critical) .. P4 (low). Lower value = more important, matching Reclaim."""

    P1 = 1
    P2 = 2
    P3 = 3
    P4 = 4


class EnergyLevel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class IntentKind(StrEnum):
    TASK = "task"
    HABIT = "habit"
    FOCUS = "focus"
    BUFFER = "buffer"
    MEETING = "meeting"


# Tie-break order when priorities are equal, mirroring Reclaim's documented
# behaviour: Smart Meetings first, then Habits, then Tasks.
KIND_RANK: dict[IntentKind, int] = {
    IntentKind.MEETING: 0,
    IntentKind.BUFFER: 1,
    IntentKind.HABIT: 2,
    IntentKind.FOCUS: 3,
    IntentKind.TASK: 4,
}


class DailyWindow(BaseModel):
    """An allowed time-of-day range, in minutes from local midnight."""

    start_min: int = Field(ge=0, le=24 * 60)
    end_min: int = Field(ge=0, le=24 * 60)

    @model_validator(mode="after")
    def _ordered(self) -> DailyWindow:
        if self.end_min <= self.start_min:
            raise ValueError(f"window end {self.end_min} must exceed start {self.start_min}")
        return self


class Subtask(BaseModel):
    id: str
    title: str = Field(min_length=1, max_length=200)
    for_date: date | None = None
    completed_at: datetime | None = None
    completed_dates: list[date] = Field(default_factory=list)


class Intent(BaseModel):
    """A demand for time that the solver must satisfy."""

    id: str
    kind: IntentKind
    title: str
    color: str | None = Field(default=None, max_length=32)
    tags: list[str] = Field(default_factory=list)
    subtasks: list[Subtask] = Field(default_factory=list)
    priority: Priority = Priority.P3
    energy: EnergyLevel | None = None

    # How much time, over what repeating period.
    minutes_per_period: int = Field(gt=0)
    period_days: int | None = Field(default=None, gt=0)
    allowed_weekdays: list[int] | None = Field(default=None, min_length=1)

    @field_validator("color")
    @classmethod
    def validate_color(cls, value: str | None) -> str | None:
        if value is not None and not (
            value
            in {
                "p1",
                "p2",
                "p3",
                "p4",
                "slate",
                "blue",
                "green",
                "red",
                "yellow",
                "purple",
                "pink",
                "orange",
            }
            or re.fullmatch(r"#[0-9a-fA-F]{6}", value)
        ):
            raise ValueError("color must be a supported palette name or #RRGGBB")
        return value

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str]) -> list[str]:
        if any(not tag.strip() for tag in value):
            raise ValueError("tags must be nonempty strings")
        return list(dict.fromkeys(value))

    @field_validator("allowed_weekdays")
    @classmethod
    def validate_allowed_weekdays(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        if any(day < 0 or day > 6 for day in value):
            raise ValueError("Weekdays must be between 0 (Monday) and 6 (Sunday)")
        if len(value) != len(set(value)):
            raise ValueError("Duplicate weekdays are not allowed")
        return value

    """None = a one-shot demand (a task). 7 = "per week" (a habit, focus goal)."""

    # Chunking. Equal min/max means non-interruptible.
    min_chunk_minutes: int = Field(gt=0)
    max_chunk_minutes: int = Field(gt=0)
    max_per_day: int | None = Field(default=None, gt=0)

    # Where in the day it may land. Empty = anywhere.
    daily_windows: list[DailyWindow] = Field(default_factory=list)

    # Absolute bounds, as slots from the horizon origin.
    slot_origin: datetime | None = None
    skipped_dates: list[date] = Field(default_factory=list)
    skipped_days: list[int] = Field(default_factory=list, exclude=True)
    earliest_slot: int | None = None
    due_slot: int | None = None

    preferred_start_min: int | None = Field(default=None, ge=0, le=24 * 60)
    """Time-of-day the user would rather this land at. A soft objective term."""

    blocked_slots: list[tuple[int, int]] = Field(default_factory=list)
    """Spans this intent alone may not occupy — the union of the other
    attendees' busy time for a Smart Meeting.

    Kept per-intent rather than folded into the global busy list because these
    are not *your* commitments: a colleague's 2pm meeting must stop this one
    meeting landing at 2pm, while leaving 2pm perfectly available for your own
    focus time. Merging the two would quietly blank out your calendar with
    other people's schedules."""

    zoom_meeting_id: str | None = None
    zoom_join_url: str | None = None
    """Set automatically for a Smart Meeting when HOROLOG_ZOOM_* is
    configured (see integrations/zoom.py). Server-set only — never accepted
    from `IntentIn`, so a client cannot fabricate a fake link. Created as a
    "no fixed time" meeting, so `zoom_join_url` stays correct regardless of
    which slot the solver places it in, or how a later re-solve moves it —
    nothing here has to stay in sync with the placement engine."""

    completed_at: datetime | None = None
    """Set once, never cleared automatically. `solver/expand.py` skips a
    completed intent entirely, so its capacity is freed on the very next
    solve — the row itself is kept rather than deleted, so the inbox and
    analytics can still show it was done. Scoped to one-shot tasks
    (`period_days is None`) at the API layer.
    """
    completed_dates: list[date] = Field(default_factory=list)
    """Local dates on which a recurring session was checked off. Kept on the
    intent so completion survives a new plan and a rolling horizon."""

    @model_validator(mode="after")
    def _coherent(self) -> Intent:
        if self.max_chunk_minutes < self.min_chunk_minutes:
            raise ValueError(
                f"max_chunk {self.max_chunk_minutes} < min_chunk {self.min_chunk_minutes}"
            )
        if self.minutes_per_period < self.min_chunk_minutes:
            raise ValueError(
                f"minutes_per_period {self.minutes_per_period} is smaller than one "
                f"min chunk ({self.min_chunk_minutes}); it could never be placed"
            )
        for field in ("minutes_per_period", "min_chunk_minutes", "max_chunk_minutes"):
            value: int = getattr(self, field)
            if value % SLOT_MINUTES:
                raise ValueError(f"{field}={value} must be a multiple of {SLOT_MINUTES} minutes")
        if (
            self.earliest_slot is not None
            and self.due_slot is not None
            and self.due_slot <= self.earliest_slot
        ):
            raise ValueError(f"due_slot {self.due_slot} must exceed earliest_slot")
        for window in self.daily_windows:
            span = window.end_min - window.start_min
            if span < self.min_chunk_minutes:
                raise ValueError(
                    f"window {window.start_min}-{window.end_min} is {span}min, shorter "
                    f"than min_chunk {self.min_chunk_minutes}; nothing could fit in it"
                )
        return self

    @property
    def fixed_weekly_sessions(self) -> bool:
        return (
            self.period_days == 7
            and self.allowed_weekdays is not None
            and self.min_chunk_minutes == self.max_chunk_minutes
            and self.max_per_day == 1
            and self.minutes_per_period == self.min_chunk_minutes * len(self.allowed_weekdays)
        )

    def for_origin(self, base: datetime) -> Intent:
        """Rebase persisted bounds at the boundary, leaving the solver calendar-free."""
        anchor = self.slot_origin or base
        if anchor.tzinfo is None:
            raise ValueError("slot_origin must be timezone-aware")
        anchor = anchor.astimezone(base.tzinfo)
        values = self.model_dump()
        for field in ("earliest_slot", "due_slot"):
            slot = getattr(self, field)
            if slot is not None:
                values[field] = to_slot(from_slot(slot, anchor), base)
        values["blocked_slots"] = [
            (to_slot(from_slot(start, anchor), base), to_slot(from_slot(end, anchor), base))
            for start, end in self.blocked_slots
        ]
        values["slot_origin"] = base
        values["skipped_days"] = [(day - base.date()).days for day in self.skipped_dates]
        return Intent.model_validate(values)

    @property
    def required_slots(self) -> int:
        return minutes_to_slots(self.minutes_per_period)

    @property
    def min_chunk_slots(self) -> int:
        return minutes_to_slots(self.min_chunk_minutes)

    @property
    def max_chunk_slots(self) -> int:
        return minutes_to_slots(self.max_chunk_minutes)
