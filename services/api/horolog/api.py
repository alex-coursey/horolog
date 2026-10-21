"""HTTP surface.

Slots stop at this boundary: everything crossing the wire is an ISO datetime.
The UI never learns that the engine thinks in 15-minute integers.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import sys
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from horolog import oauth
from horolog.analytics import Analytics, analyse
from horolog.capture import capture, to_payload
from horolog.db import (
    BusyRow,
    CalendarBlockRow,
    CreationRequestRow,
    IntentRow,
    SyncedBlockRow,
    init_db,
    load_intents,
    load_previous_plan,
    save_plan,
    session,
)
from horolog.domain.events import BusyInterval
from horolog.domain.intent import DailyWindow, EnergyLevel, Intent, IntentKind, Priority, Subtask
from horolog.domain.plan import Plan
from horolog.domain.time import (
    SLOT_MINUTES,
    SLOTS_PER_DAY,
    from_slot,
    minutes_to_slots,
    to_slot,
)
from horolog.integrations.clickup import ClickUpError, fetch_clickup_tasks
from horolog.integrations.github import GithubError, fetch_github_issues
from horolog.integrations.google_calendar import (
    GoogleCalendarProvider,
    GoogleCalendarWriter,
)
from horolog.integrations.jira import JiraError, fetch_jira_issues
from horolog.integrations.linear import LinearError, fetch_linear_issues
from horolog.integrations.notion import NotionError, fetch_notion_tasks
from horolog.integrations.outlook_calendar import (
    OutlookCalendarProvider,
    OutlookCalendarWriter,
)
from horolog.integrations.todoist import TodoistError, fetch_todoist_tasks
from horolog.integrations.zoom import ZoomError, create_meeting, delete_meeting
from horolog.llm import (
    AnthropicProvider,
    ExtractionFailed,
    OpenAICompatible,
    Provider,
    ProviderError,
)
from horolog.providers import (
    BUFFER_SOURCE,
    CalDAVProvider,
    CalendarProvider,
    ICSProvider,
    SyncError,
    decompression_buffers,
    to_ics,
)
from horolog.settings import settings
from horolog.solver.solve import merge_busy, solve

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Time origin
# --------------------------------------------------------------------------


def origin() -> datetime:
    """Midnight today in the configured zone.

    Recomputed per request rather than pinned at boot, so a process that stays
    up across midnight does not keep scheduling into yesterday.
    """
    now = datetime.now(settings().zone)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def horizon_slots() -> int:
    return settings().horizon_days * SLOTS_PER_DAY


MAX_PUSH_OPS = 200
"""Hard cap on create/patch/delete calls in one calendar push — same
convention as `integrations/notion.py`'s `MAX_PAGES`. A first push against a
large plan must not exhaust the provider's write quota in one call; whatever
does not fit catches up on the next background tick or the next explicit
push, since the diff against `synced_blocks` is what decides what is left."""


def _clip(start: int, end: int) -> tuple[int, int] | None:
    """Trim a span to the horizon, or None if it falls entirely outside it.

    Guards the write path. `BusyInterval` refuses a negative slot, so storing a
    yesterday event — trivially posted by a client in a timezone behind the
    server, or by anyone entering a meeting that has already begun — used to
    make every later read of the plan raise on the way out. The row could then
    only be removed from the database by hand. Feed events have always been
    clipped in `_to_interval`; the hand-entered path just never was.
    """
    lo, hi = max(start, 0), min(end, horizon_slots())
    return (lo, hi) if hi > lo else None


def _localise(value: datetime | None) -> datetime | None:
    """Attach the configured zone to a naive datetime.

    ICS files and plenty of calendar clients emit "floating" local times with no
    offset, and a browser's `toISOString()` drops the zone too. The engine
    requires aware datetimes, so without normalising here every such input
    reaches `to_slot` and raises — a 500 for input that is entirely ordinary.

    Applied as a field validator on every wire model rather than at each call
    site, so a new datetime field cannot reintroduce the bug by omission.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=settings().zone)


LocalDateTime = Annotated[datetime, AfterValidator(_localise)]


# --------------------------------------------------------------------------
# Wire contracts
# --------------------------------------------------------------------------


class IntentIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    color: str | None = Field(default=None, max_length=32)
    tags: list[str] = Field(default_factory=list)
    kind: IntentKind = IntentKind.TASK
    priority: Priority = Priority.P3
    energy: EnergyLevel | None = None
    minutes_per_period: int = Field(gt=0)
    period_days: int | None = Field(default=None, gt=0)
    allowed_weekdays: list[int] | None = Field(default=None, min_length=1)

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

    min_chunk_minutes: int = Field(default=30, gt=0)
    max_chunk_minutes: int = Field(default=120, gt=0)
    max_per_day: int | None = Field(default=None, gt=0)
    window_start_min: int | None = None
    window_end_min: int | None = None
    due: LocalDateTime | None = None
    earliest: LocalDateTime | None = None
    preferred_start_min: int | None = None

    attendee_busy: list[AttendeeBusy] = Field(default_factory=list)
    """Other attendees' commitments, for a Smart Meeting.

    Constrains this intent only. Deliberately not merged into the shared busy
    mirror: a colleague being booked at 2pm must stop *this meeting* landing at
    2pm without blanking 2pm out for your own focus time."""

    def to_domain(self, ident: str, base: datetime) -> Intent:
        cfg = settings()
        start = (
            self.window_start_min if self.window_start_min is not None else cfg.workday_start_min
        )
        end = self.window_end_min if self.window_end_min is not None else cfg.workday_end_min
        return Intent(
            id=ident,
            slot_origin=base,
            kind=self.kind,
            title=self.title,
            color=self.color,
            tags=self.tags,
            priority=self.priority,
            energy=self.energy,
            minutes_per_period=self.minutes_per_period,
            period_days=self.period_days,
            allowed_weekdays=self.allowed_weekdays,
            min_chunk_minutes=self.min_chunk_minutes,
            max_chunk_minutes=self.max_chunk_minutes,
            max_per_day=self.max_per_day,
            daily_windows=[DailyWindow(start_min=start, end_min=end)],
            earliest_slot=to_slot(self.earliest, base, round_up=True) if self.earliest else None,
            due_slot=to_slot(self.due, base) if self.due else None,
            preferred_start_min=self.preferred_start_min,
            blocked_slots=[
                (to_slot(a.start, base), to_slot(a.end, base, round_up=True))
                for a in self.attendee_busy
            ],
        )


class IntentPatch(IntentIn):
    """IntentIn wire fields; omitted defaults are never applied on PATCH."""

    model_config = {"extra": "forbid"}
    title: str = Field(default="", min_length=1, max_length=200)
    minutes_per_period: int = Field(default=0, gt=0)
    daily_windows: list[DailyWindow] = Field(default_factory=list)
    add_tags: list[str] = Field(default_factory=list)
    remove_tags: list[str] = Field(default_factory=list)
    session_minutes: int | None = Field(default=None, gt=0)


class AttendeeBusy(BaseModel):
    """One span in which some other attendee is unavailable."""

    start: LocalDateTime
    end: LocalDateTime
    attendee: str = ""

    @model_validator(mode="after")
    def ordered(self) -> AttendeeBusy:
        if self.end.astimezone(UTC) <= self.start.astimezone(UTC):
            raise ValueError("attendee busy end must exceed start")
        return self


class BusyIn(BaseModel):
    label: str = ""
    start: LocalDateTime
    end: LocalDateTime
    source: str = "manual"


class CalendarBlockIn(BaseModel):
    model_config = {"extra": "forbid"}
    title: str = Field(min_length=1, max_length=200)
    start: LocalDateTime | None = None
    end: LocalDateTime | None = None
    all_day_date: date | None = None
    end_date: date | None = None

    def bounds(self) -> tuple[datetime, datetime]:
        zone = settings().zone
        if not self.title.strip():
            raise ValueError("title must not be blank")
        if self.all_day_date is not None:
            if self.start is not None or self.end is not None:
                raise ValueError("use all_day_date or start/end, not both")
            end_date = self.end_date or self.all_day_date + timedelta(days=1)
            start = datetime.combine(self.all_day_date, datetime.min.time(), tzinfo=zone)
            end = datetime.combine(end_date, datetime.min.time(), tzinfo=zone)
        else:
            if self.start is None or self.end is None or self.end_date is not None:
                raise ValueError("supply both start and end, or all_day_date")
            start, end = self.start.astimezone(zone), self.end.astimezone(zone)
        if end.astimezone(UTC) <= start.astimezone(UTC):
            raise ValueError("end must exceed start; end_date is exclusive")
        return start, end


def _calendar_block_bounds(row: CalendarBlockRow) -> tuple[datetime, datetime]:
    # SQLite drops timezone metadata; persisted values are always UTC.
    zone = settings().zone
    return (
        row.start_at.replace(tzinfo=UTC).astimezone(zone),
        row.end_at.replace(tzinfo=UTC).astimezone(zone),
    )


def _calendar_block_out(row: CalendarBlockRow) -> dict[str, Any]:
    start, end = _calendar_block_bounds(row)
    return {
        "id": row.id,
        "title": row.title,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "all_day": row.all_day,
        "source": "calendar-block",
    }


class BlockOut(BaseModel):
    intent_id: str
    color: str | None = None
    tags: list[str] = Field(default_factory=list)
    title: str
    kind: IntentKind
    priority: Priority
    energy: EnergyLevel | None = None
    occurrence: int
    chunk: int
    start: datetime
    end: datetime
    moved_from: datetime | None = None
    deadline: datetime | None = None
    late_by_minutes: int = 0
    completed: bool = False
    recurring: bool = False


class UnmetOut(BaseModel):
    intent_id: str
    title: str
    priority: Priority
    occurrence: int
    shortfall_minutes: int


class PlanOut(BaseModel):
    blocks: list[BlockOut]
    unmet: list[UnmetOut]
    busy: list[BusyIn]
    solve_ms: float
    complete: bool
    generated_at: datetime
    origin: datetime
    horizon_days: int


# --------------------------------------------------------------------------
# Change stream
# --------------------------------------------------------------------------


class Broadcast:
    """Fan-out to connected SSE clients.

    ponytail: in-process only. Multiple API workers each hold their own
    subscriber set, so a change on worker A never reaches a client attached to
    worker B. Move to Postgres LISTEN/NOTIFY when running more than one process.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[str]]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=16)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    def publish(self, event: str) -> None:
        for queue in list(self._subscribers):
            # A client too slow to keep up is dropped from this message rather
            # than allowed to block the request that produced it.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)


bus = Broadcast()


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await init_db()
    sync_task = asyncio.create_task(_sync_loop())
    try:
        yield
    finally:
        sync_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sync_task


app = FastAPI(title="Horolog", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings().cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health(db: AsyncSession = Depends(session)) -> dict[str, str]:
    # A process that answers HTTP but can't reach its database is not
    # healthy — Docker's healthcheck (and anything restarting on it) needs
    # to see that, not a static 200 that's identical whether Postgres is up.
    try:
        await db.execute(select(1))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unreachable: {exc}") from exc
    return {"status": "ok"}


async def _reserve_creation(
    db: AsyncSession, scope: str, key: str | None, payload: dict[str, Any], ident: str
) -> tuple[str, bool]:
    if key is None:
        return ident, False
    if not key.strip():
        raise HTTPException(status_code=422, detail="Idempotency-Key must not be blank")
    request_id = hashlib.sha256(f"{scope}:{key}".encode()).hexdigest()
    payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    request = CreationRequestRow(id=request_id, payload_hash=payload_hash, resource_id=ident)
    db.add(request)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        existing = await db.get(CreationRequestRow, request_id)
        if existing is None or existing.payload_hash != payload_hash:
            raise HTTPException(
                status_code=409, detail="retry key already used for different input"
            ) from None
        return existing.resource_id, True
    return ident, False


@app.get("/api/intents")
async def list_intents(db: AsyncSession = Depends(session)) -> list[dict[str, Any]]:
    rows = (await db.execute(select(IntentRow))).scalars().all()
    return [row.to_domain().for_origin(origin()).model_dump(mode="json") for row in rows]


@app.post("/api/intents", status_code=201)
async def create_intent(
    body: IntentIn,
    db: AsyncSession = Depends(session),
    idempotency_key: Annotated[str | None, Header(max_length=128)] = None,
) -> dict[str, Any]:
    ident = uuid.uuid4().hex[:12]
    try:
        intent = body.to_domain(ident, origin())
    except ValueError as exc:
        # Domain validation is the real gate; surface its message rather than a
        # generic 500, because it explains exactly why the intent is impossible.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    ident, reused = await _reserve_creation(
        db, "intent", idempotency_key, body.model_dump(mode="json"), ident
    )
    if reused:
        existing = await db.get(IntentRow, ident)
        if existing is None:
            raise HTTPException(status_code=409, detail="original task has been deleted")
        await _replan(db)
        return existing.to_domain().for_origin(origin()).model_dump(mode="json")
    if intent.kind == IntentKind.MEETING:
        cfg = settings()
        if cfg.zoom_account_id and cfg.zoom_client_id and cfg.zoom_client_secret:
            # Best-effort: a Zoom outage or bad credential must never stop a
            # meeting from being scheduled, only leave it without a link.
            try:
                meeting = await create_meeting(cfg, intent.title)
                intent = intent.model_copy(
                    update={"zoom_meeting_id": meeting.id, "zoom_join_url": meeting.join_url}
                )
            except ZoomError as exc:
                logger.warning("Zoom meeting creation failed for %r: %s", intent.title, exc)

    db.add(IntentRow(id=ident, payload=intent.model_dump(mode="json")))
    await db.commit()
    await _replan(db)
    return intent.model_dump(mode="json")


class CaptureIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    model: str | None = None
    provider: str | None = None
    api_key: str | None = None


@app.post("/api/capture", status_code=201)
async def capture_intent(body: CaptureIn, db: AsyncSession = Depends(session)) -> dict[str, Any]:
    """Natural language in, a scheduled intent out.

    The model's output is a proposal: it is schema-constrained at decode time,
    Pydantic-validated here, and then re-validated by the domain model through
    the ordinary create path. It never reaches a calendar except by way of the
    placer, so the worst a bad extraction can do is create a wrong-looking
    intent the user can delete.
    """
    try:
        custom_provider: Provider | None = None
        if body.provider and body.provider != "default" and body.model:
            timeout = settings().llm_timeout_s
            if body.provider == "anthropic":
                # Raises a friendly RuntimeError if the optional `anthropic`
                # extra isn't installed — has to happen inside this try, or
                # that message never reaches the response.
                custom_provider = AnthropicProvider(body.model, body.api_key or "", timeout)
            else:
                base_url = (
                    "https://api.openai.com/v1"
                    if body.provider == "openai"
                    else settings().llm_base_url
                )
                custom_provider = OpenAICompatible(
                    base_url, body.model, body.api_key or "", timeout
                )

        draft = await capture(body.text, provider=custom_provider)
    except ExtractionFailed as exc:
        # Explicitly not a 500: the request was fine, the model could not read
        # it. The UI falls back to the manual form on this status.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ProviderError, httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=f"language model unreachable: {exc}") from exc

    payload = to_payload(draft, origin())
    created = await create_intent(IntentIn.model_validate(payload), db)
    return {"intent": created, "understood": draft.model_dump(mode="json")}


@app.delete("/api/intents/{intent_id}")
async def delete_intent(intent_id: str, db: AsyncSession = Depends(session)) -> Response:
    row = await db.get(IntentRow, intent_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no intent {intent_id!r}")
    zoom_meeting_id = row.payload.get("zoom_meeting_id")
    await db.delete(row)
    await db.commit()

    if zoom_meeting_id:
        cfg = settings()
        if cfg.zoom_account_id and cfg.zoom_client_id and cfg.zoom_client_secret:
            # Best-effort cleanup — an unreachable Zoom must never stop the
            # intent itself from being deleted; it just leaves an orphaned
            # meeting behind in the Zoom account.
            try:
                await delete_meeting(cfg, zoom_meeting_id)
            except ZoomError as exc:
                logger.warning("Could not remove Zoom meeting %s: %s", zoom_meeting_id, exc)

    await _replan(db)
    return Response(status_code=204)


@app.post("/api/intents/{intent_id}/complete")
async def complete_intent(intent_id: str, db: AsyncSession = Depends(session)) -> dict[str, Any]:
    """Mark a one-shot task done.

    The row is kept rather than deleted — so the inbox and analytics can
    still show it was finished — but `solver/expand.py` skips a completed
    intent entirely, so whatever capacity it still held is freed on the very
    next solve, same as a delete would free it.
    """
    row = await db.get(IntentRow, intent_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no intent {intent_id!r}")
    intent = Intent.model_validate(row.payload)
    if intent.period_days is not None:
        raise HTTPException(
            status_code=422,
            detail="use a session date to complete a recurring task",
        )
    finished = datetime.now(UTC)
    intent = intent.model_copy(update={
        "completed_at": finished,
        "subtasks": [item.model_copy(update={"completed_at": finished}) for item in intent.subtasks],
    })
    row.payload = intent.model_dump(mode="json")
    await db.commit()
    await _replan(db)
    return intent.model_dump(mode="json")


@app.delete("/api/intents/{intent_id}/complete")
async def uncomplete_intent(intent_id: str, db: AsyncSession = Depends(session)) -> dict[str, Any]:
    """Undo a completion — a misclick shouldn't require a delete-and-retype."""
    row = await db.get(IntentRow, intent_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no intent {intent_id!r}")
    previous = Intent.model_validate(row.payload)
    intent = previous.model_copy(update={
        "completed_at": None,
        "subtasks": [item.model_copy(update={"completed_at": None}) for item in previous.subtasks],
    })
    row.payload = intent.model_dump(mode="json")
    await db.commit()
    await _replan(db)
    return intent.model_dump(mode="json")


@app.put("/api/intents/{intent_id}/completed-dates/{day}")
async def complete_session(
    intent_id: str, day: date, db: AsyncSession = Depends(session)
) -> dict[str, Any]:
    row = await db.get(IntentRow, intent_id, with_for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    intent = row.to_domain()
    if intent.period_days is None:
        raise HTTPException(status_code=422, detail="use task completion for a one-time task")
    intent = intent.model_copy(update={
        "completed_dates": sorted({*intent.completed_dates, day}),
        "subtasks": [item.model_copy(update={"completed_dates": sorted({*item.completed_dates, day})}) if item.for_date in (None, day) else item for item in intent.subtasks],
    })
    row.payload = intent.model_dump(mode="json")
    await db.commit()
    bus.publish(json.dumps({"intent_id": intent_id, "completed_date": day.isoformat()}))
    return intent.model_dump(mode="json")


@app.delete("/api/intents/{intent_id}/completed-dates/{day}")
async def uncomplete_session(
    intent_id: str, day: date, db: AsyncSession = Depends(session)
) -> dict[str, Any]:
    row = await db.get(IntentRow, intent_id, with_for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    intent = row.to_domain()
    if intent.period_days is None:
        raise HTTPException(status_code=422, detail="use task completion for a one-time task")
    intent = intent.model_copy(update={
        "completed_dates": [d for d in intent.completed_dates if d != day],
        "subtasks": [item.model_copy(update={"completed_dates": [d for d in item.completed_dates if d != day]}) if item.for_date in (None, day) else item for item in intent.subtasks],
    })
    row.payload = intent.model_dump(mode="json")
    await db.commit()
    bus.publish(json.dumps({"intent_id": intent_id, "completed_date": day.isoformat()}))
    return intent.model_dump(mode="json")


class SubtaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def nonblank_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("subtask title cannot be blank")
        return value


@app.post("/api/intents/{intent_id}/subtasks", status_code=201)
async def add_subtask(
    intent_id: str, body: SubtaskIn, day: date | None = None,
    db: AsyncSession = Depends(session),
) -> dict[str, Any]:
    row = await db.get(IntentRow, intent_id, with_for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    intent = row.to_domain()
    if len(intent.subtasks) >= 100:
        raise HTTPException(status_code=422, detail="a task can have at most 100 subtasks")
    if intent.period_days is not None and day is None:
        raise HTTPException(status_code=422, detail="a date is required for recurring tasks")
    item = Subtask(
        id=uuid.uuid4().hex[:12], title=body.title, for_date=day,
    )
    updates: dict[str, Any] = {"subtasks": [*intent.subtasks, item]}
    replan = False
    if intent.period_days is None and intent.completed_at is not None:
        updates["completed_at"] = None
        replan = True
    elif day in intent.completed_dates:
        updates["completed_dates"] = [d for d in intent.completed_dates if d != day]
    intent = intent.model_copy(update=updates)
    row.payload = intent.model_dump(mode="json")
    await db.commit()
    if replan:
        await _replan(db)
    else:
        bus.publish(json.dumps({"intent_id": intent_id}))
    return intent.model_dump(mode="json")


@app.put("/api/intents/{intent_id}/subtasks/{subtask_id}/complete")
@app.delete("/api/intents/{intent_id}/subtasks/{subtask_id}/complete")
async def set_subtask_completion(
    intent_id: str, subtask_id: str, request: Request, day: date | None = None,
    db: AsyncSession = Depends(session),
) -> dict[str, Any]:
    row = await db.get(IntentRow, intent_id, with_for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    intent = row.to_domain()
    matching = next((item for item in intent.subtasks if item.id == subtask_id), None)
    if matching is None:
        raise HTTPException(status_code=404, detail="subtask not found")
    if intent.period_days is not None and day is None:
        raise HTTPException(status_code=422, detail="a date is required for recurring tasks")
    if matching.for_date is not None and matching.for_date != day:
        raise HTTPException(status_code=422, detail="this goal belongs to another date")
    complete = request.method == "PUT"
    finished = datetime.now(UTC)
    subtasks = []
    for item in intent.subtasks:
        if item.id != subtask_id:
            subtasks.append(item)
        elif intent.period_days is None:
            subtasks.append(item.model_copy(update={"completed_at": finished if complete else None}))
        else:
            dates = ({*item.completed_dates, day} if complete else set(item.completed_dates) - {day})
            subtasks.append(item.model_copy(update={"completed_dates": sorted(dates)}))
    updates: dict[str, Any] = {"subtasks": subtasks}
    replan = False
    if intent.period_days is None:
        all_done = all(item.completed_at is not None for item in subtasks)
        if all_done != (intent.completed_at is not None):
            updates["completed_at"] = finished if all_done else None
            replan = True
    else:
        dates = set(intent.completed_dates)
        active = [item for item in subtasks if item.for_date in (None, day)]
        if active and all(day in item.completed_dates for item in active):
            dates.add(day)
        else:
            dates.discard(day)
        updates["completed_dates"] = sorted(dates)
    intent = intent.model_copy(update=updates)
    row.payload = intent.model_dump(mode="json")
    await db.commit()
    if replan:
        await _replan(db)
    else:
        bus.publish(json.dumps({"intent_id": intent_id}))
    return intent.model_dump(mode="json")


@app.delete("/api/intents/{intent_id}/subtasks/{subtask_id}")
async def remove_subtask(
    intent_id: str, subtask_id: str, db: AsyncSession = Depends(session)
) -> dict[str, Any]:
    row = await db.get(IntentRow, intent_id, with_for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    intent = row.to_domain()
    subtasks = [item for item in intent.subtasks if item.id != subtask_id]
    if len(subtasks) == len(intent.subtasks):
        raise HTTPException(status_code=404, detail="subtask not found")
    updates: dict[str, Any] = {"subtasks": subtasks}
    replan = False
    if intent.period_days is None and subtasks and all(item.completed_at for item in subtasks):
        if intent.completed_at is None:
            updates["completed_at"] = datetime.now(UTC)
            replan = True
    intent = intent.model_copy(update=updates)
    row.payload = intent.model_dump(mode="json")
    await db.commit()
    if replan:
        await _replan(db)
    else:
        bus.publish(json.dumps({"intent_id": intent_id}))
    return intent.model_dump(mode="json")


@app.get("/api/intents/{intent_id}")
async def get_intent(intent_id: str, db: AsyncSession = Depends(session)) -> dict[str, Any]:
    row = await db.get(IntentRow, intent_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no intent {intent_id!r}")
    return row.to_domain().for_origin(origin()).model_dump(mode="json")


@app.patch("/api/intents/{intent_id}")
async def patch_intent(
    intent_id: str, body: IntentPatch, db: AsyncSession = Depends(session)
) -> dict[str, Any]:
    """Merge supplied wire fields, then validate the complete Intent.

    Tags replaces the list; add_tags and remove_tags apply afterwards (remove wins).
    daily_windows replaces all windows; window_start/end_min edits the first window.
    Explicit null clears nullable bounds, recurrence, weekdays, or color.
    """
    row = await db.get(IntentRow, intent_id, with_for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no intent {intent_id!r}")
    base = origin()
    previous = row.to_domain().for_origin(base)
    updates = body.model_dump(exclude_unset=True)
    try:
        for wire, stored in (("due", "due_slot"), ("earliest", "earliest_slot")):
            if wire in updates:
                value = updates.pop(wire)
                updates[stored] = (
                    to_slot(value, base, round_up=wire == "earliest") if value is not None else None
                )
        window_fields = {"window_start_min", "window_end_min"} & updates.keys()
        if window_fields:
            if "daily_windows" in updates:
                raise ValueError("use daily_windows or window_start/end_min, not both")
            windows = [w.model_dump() for w in previous.daily_windows]
            if not windows:
                windows = [{"start_min": 0, "end_min": 1440}]
            for wire in window_fields:
                value = updates.pop(wire)
                if value is None:
                    raise ValueError("use daily_windows=[] to clear time windows")
                windows[0][wire.removeprefix("window_")] = value
            updates["daily_windows"] = windows
        if "attendee_busy" in updates:
            updates.pop("attendee_busy")
            updates["blocked_slots"] = [
                (to_slot(a.start, base), to_slot(a.end, base, round_up=True))
                for a in body.attendee_busy
            ]
        additions = updates.pop("add_tags", [])
        removals = updates.pop("remove_tags", [])
        if additions or removals:
            updates["tags"] = list(
                dict.fromkeys(
                    tag
                    for tag in [*updates.get("tags", previous.tags), *additions]
                    if tag not in removals
                )
            )
        session = updates.pop("session_minutes", None)
        if session is not None or (
            "allowed_weekdays" in updates
            and "minutes_per_period" not in updates
            and previous.fixed_weekly_sessions
            and updates.get("period_days", previous.period_days) == 7
            and updates["allowed_weekdays"]
        ):
            if (
                "minutes_per_period" in updates
                or {"min_chunk_minutes", "max_chunk_minutes"} & updates.keys()
            ):
                raise ValueError(
                    "session_minutes cannot be combined with demand or chunk overrides"
                )
            weekdays = updates.get("allowed_weekdays", previous.allowed_weekdays)
            if updates.get("period_days", previous.period_days) != 7 or not weekdays:
                raise ValueError("session editing requires weekly fixed sessions and weekdays")
            session = session or previous.min_chunk_minutes
            updates.update(
                minutes_per_period=session * len(weekdays),
                min_chunk_minutes=session,
                max_chunk_minutes=session,
                max_per_day=1,
            )
        intent = Intent.model_validate(previous.model_dump() | updates)
        if intent.skipped_dates and not intent.fixed_weekly_sessions:
            raise ValueError("restore skipped sessions before changing the recurrence shape")
        if intent.completed_at is not None and intent.period_days is not None:
            raise ValueError("uncomplete a task before converting it to recurrence")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    row.payload = intent.model_dump(mode="json")
    await db.commit()
    if updates.keys() - {"title", "color", "tags"}:
        await _replan(db)
    else:
        bus.publish(json.dumps({"intent_id": intent_id}))
    return intent.model_dump(mode="json")


@app.put("/api/intents/{intent_id}")
async def update_intent(
    intent_id: str, body: IntentIn, db: AsyncSession = Depends(session)
) -> dict[str, Any]:
    """Replace an intent in place, keeping its id.

    Full replace rather than a per-field merge: fifteen interdependent fields
    behind one cross-field validator make merge logic more code and more edge
    cases than just re-validating the whole object, and the frontend already
    has the whole thing in hand when it opens an edit form.

    Keeping the id is the actual point — the previous plan is keyed by it, so
    an edit keeps placement stability for every chunk still legal under the
    new shape, where delete-and-recreate would throw all of it away and
    reshuffle blocks that never needed to move.
    """
    row = await db.get(IntentRow, intent_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no intent {intent_id!r}")
    try:
        intent = body.to_domain(intent_id, origin())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Neither field is settable from the wire — a Zoom link is server-issued
    # on create, and completion has its own dedicated routes — so an edit
    # must not silently wipe either as a side effect of `to_domain` defaulting
    # them to None.
    previous = Intent.model_validate(row.payload)
    intent = intent.model_copy(
        update={
            "zoom_meeting_id": previous.zoom_meeting_id,
            "zoom_join_url": previous.zoom_join_url,
            "completed_at": previous.completed_at,
            "completed_dates": previous.completed_dates,
            "subtasks": previous.subtasks,
            "skipped_dates": previous.skipped_dates,
        }
    )
    if intent.completed_at is not None and intent.period_days is not None:
        raise HTTPException(
            status_code=422, detail="uncomplete a task before converting to recurrence"
        )
    if intent.skipped_dates and not intent.fixed_weekly_sessions:
        raise HTTPException(
            status_code=422, detail="restore skipped sessions before changing recurrence"
        )
    row.payload = intent.model_dump(mode="json")
    await db.commit()
    await _replan(db)
    return intent.model_dump(mode="json")


@app.post("/api/intents/{intent_id}/skips/{skipped_date}")
async def skip_session(
    intent_id: str, skipped_date: date, db: AsyncSession = Depends(session)
) -> dict[str, Any]:
    row = await db.get(IntentRow, intent_id, with_for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    intent = row.to_domain().for_origin(origin())
    if not intent.fixed_weekly_sessions:
        raise HTTPException(status_code=422, detail="skipping requires fixed weekly sessions")
    if skipped_date < origin().date() or skipped_date.weekday() not in (
        intent.allowed_weekdays or []
    ):
        raise HTTPException(
            status_code=422, detail="choose a future/today date on an allowed weekday"
        )
    intent = Intent.model_validate(
        intent.model_dump() | {"skipped_dates": sorted({*intent.skipped_dates, skipped_date})}
    )
    row.payload = intent.model_dump(mode="json")
    await db.commit()
    return {
        "task": intent.model_dump(mode="json"),
        "plan": (await _render(db, await _replan(db))).model_dump(mode="json"),
    }


@app.delete("/api/intents/{intent_id}/skips/{skipped_date}")
async def restore_session(
    intent_id: str, skipped_date: date, db: AsyncSession = Depends(session)
) -> dict[str, Any]:
    row = await db.get(IntentRow, intent_id, with_for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    intent = row.to_domain()
    row.payload = intent.model_copy(
        update={"skipped_dates": [day for day in intent.skipped_dates if day != skipped_date]}
    ).model_dump(mode="json")
    await db.commit()
    return {
        "task": row.to_domain().for_origin(origin()).model_dump(mode="json"),
        "plan": (await _render(db, await _replan(db))).model_dump(mode="json"),
    }


@app.get("/api/calendar-blocks")
async def list_calendar_blocks(db: AsyncSession = Depends(session)) -> list[dict[str, Any]]:
    rows = (
        (
            await db.execute(
                select(CalendarBlockRow).order_by(CalendarBlockRow.start_at, CalendarBlockRow.id)
            )
        )
        .scalars()
        .all()
    )
    return [_calendar_block_out(row) for row in rows]


@app.post("/api/calendar-blocks", status_code=201)
async def create_calendar_block(
    body: CalendarBlockIn,
    db: AsyncSession = Depends(session),
    idempotency_key: Annotated[str | None, Header(max_length=128)] = None,
) -> dict[str, Any]:
    """Add one fixed commitment without replacing any existing busy events."""
    try:
        start, end = body.bounds()
    except (ValueError, OverflowError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    ident, reused = await _reserve_creation(
        db, "calendar-block", idempotency_key, body.model_dump(mode="json"), uuid.uuid4().hex
    )
    if reused:
        existing = await db.get(CalendarBlockRow, ident)
        if existing is None:
            raise HTTPException(status_code=409, detail="original block has been deleted")
        return _calendar_block_out(existing) | {
            "plan": (await _render(db, await _replan(db))).model_dump(mode="json")
        }
    row = CalendarBlockRow(
        id=ident,
        title=body.title,
        start_at=start.astimezone(UTC),
        end_at=end.astimezone(UTC),
        all_day=body.all_day_date is not None,
    )
    db.add(row)
    await db.commit()
    plan = await _replan(db)
    return _calendar_block_out(row) | {"plan": (await _render(db, plan)).model_dump(mode="json")}


class CalendarBlockPatch(CalendarBlockIn):
    title: str = Field(default="", min_length=1, max_length=200)


@app.patch("/api/calendar-blocks/{block_id}")
async def update_calendar_block(
    block_id: str, body: CalendarBlockPatch, db: AsyncSession = Depends(session)
) -> dict[str, Any]:
    row = await db.get(CalendarBlockRow, block_id, with_for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail="calendar block not found")
    start, end = _calendar_block_bounds(row)
    changes = body.model_dump(exclude_unset=True)
    previous: dict[str, Any] = {"title": row.title}
    if row.all_day:
        previous.update(all_day_date=start.date(), end_date=end.date())
        if "all_day_date" in changes and "end_date" not in changes:
            try:
                changes["end_date"] = (
                    changes["all_day_date"] + (end.date() - start.date())
                    if changes["all_day_date"]
                    else None
                )
            except OverflowError as exc:
                raise HTTPException(
                    status_code=422, detail="end date is outside the supported range"
                ) from exc
        if "start" in changes or "end" in changes:
            previous.update(all_day_date=None, end_date=None)
    else:
        previous.update(start=start, end=end)
        if "all_day_date" in changes:
            previous.update(start=None, end=None)
    try:
        merged = CalendarBlockIn.model_validate(previous | changes)
        start, end = merged.bounds()
    except (ValueError, OverflowError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    row.title, row.start_at, row.end_at = merged.title, start.astimezone(UTC), end.astimezone(UTC)
    row.all_day = merged.all_day_date is not None
    await db.commit()
    return _calendar_block_out(row) | {
        "plan": (await _render(db, await _replan(db))).model_dump(mode="json")
    }


@app.delete("/api/calendar-blocks/{block_id}", status_code=204)
async def delete_calendar_block(block_id: str, db: AsyncSession = Depends(session)) -> Response:
    row = await db.get(CalendarBlockRow, block_id)
    if row is None:
        raise HTTPException(status_code=404, detail="calendar block not found")
    await db.delete(row)
    await db.commit()
    await _replan(db)
    return Response(status_code=204)


@app.put("/api/busy", status_code=200)
async def replace_busy(body: list[BusyIn], db: AsyncSession = Depends(session)) -> dict[str, int]:
    """Replace the hand-entered calendar.

    Stands in for the provider sync layer: whatever is posted becomes the set of
    immovable events. Replacing rather than merging keeps the mirror an exact
    reflection, so a deleted meeting actually frees its slot.

    Scoped to the sources being written. A blanket delete here would silently
    wipe a synced ICS feed and every accepted booking the moment anyone saved a
    manual event — sources are independent mirrors, and each owns only its own
    rows (`_mirror` scopes its delete the same way).
    """
    base = origin()
    touched = {event.source for event in body} | {"manual"}
    await db.execute(delete(BusyRow).where(BusyRow.source.in_(touched)))
    stored = 0
    for event in body:
        if event.end <= event.start:
            raise HTTPException(status_code=422, detail=f"{event.label!r} ends before it starts")
        span = _clip(to_slot(event.start, base), to_slot(event.end, base))
        if span is None:
            continue
        stored += 1
        db.add(
            BusyRow(
                id=uuid.uuid4().hex[:16],
                source=event.source,
                label=event.label,
                start_slot=span[0],
                end_slot=span[1],
            )
        )
    await db.commit()
    plan = await _replan(db)
    return {"events": stored, "blocks": len(plan.blocks)}


@app.get("/api/plan")
async def get_plan(db: AsyncSession = Depends(session)) -> PlanOut:
    plan = await load_previous_plan(db)
    if plan is None:
        plan = await _replan(db)
    return await _render(db, plan)


@app.post("/api/plan/solve")
async def resolve(db: AsyncSession = Depends(session)) -> PlanOut:
    return await _render(db, await _replan(db))


@app.get("/api/analytics")
async def analytics(db: AsyncSession = Depends(session)) -> Analytics:
    """How the plan actually spends the week."""
    cfg = settings()
    plan = await load_previous_plan(db) or await _replan(db)
    return analyse(
        plan,
        await load_intents(db),
        # Merged, because a double-booked calendar would otherwise count the
        # same hour twice and report a meeting load above 100%.
        merge_busy(await _busy(db)),
        horizon_days=cfg.horizon_days,
        workday_start_min=cfg.workday_start_min,
        workday_end_min=cfg.workday_end_min,
    )


class IcsSyncIn(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


@app.post("/api/sync/ics")
async def sync_ics(body: IcsSyncIn, db: AsyncSession = Depends(session)) -> dict[str, int]:
    """Mirror a published .ics feed.

    The zero-config path onto Google and Outlook: both publish a private iCal
    address, so this needs no OAuth app and no publicly reachable callback.
    """
    cfg = settings()
    provider = ICSProvider(body.url, cfg.zone)
    return await _mirror(db, provider, "ics")


class CalDavSyncIn(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    username: str = ""
    password: str = ""


@app.post("/api/sync/caldav")
async def sync_caldav(body: CalDavSyncIn, db: AsyncSession = Depends(session)) -> dict[str, int]:
    cfg = settings()
    provider = CalDAVProvider(body.url, body.username, body.password, cfg.zone)
    return await _mirror(db, provider, "caldav")


LINEAR_PREFIX = "linear:"
TODOIST_PREFIX = "todoist:"
GITHUB_PREFIX = "github:"
NOTION_PREFIX = "notion:"
CLICKUP_PREFIX = "clickup:"
JIRA_PREFIX = "jira:"


class LinearSyncIn(BaseModel):
    api_key: str = Field(default="", max_length=200)
    """A personal API key, pasted directly. Empty uses the stored OAuth
    connection instead — either path is a legitimate way to authenticate."""
    priority: Priority = Priority.P2
    max_chunk_minutes: int = Field(default=120, gt=0)


async def _resolve_credential(db: AsyncSession, provider: str, pasted: str) -> str:
    """A pasted key wins; otherwise fall back to a stored OAuth connection.

    Stripped, not just checked for emptiness: a key copied from a terminal
    or a text field routinely carries a trailing newline or space, and that
    one invisible character turns into `Authorization: Bearer <key> ` — a
    header value httpx's own validation rejects outright, well before the
    request ever reaches the provider. A whitespace-only paste is treated as
    no paste at all, falling through to a stored OAuth connection same as
    an empty one would.
    """
    pasted = pasted.strip()
    if pasted:
        return pasted
    token = await oauth.valid_access_token(db, settings(), provider)
    if not token:
        raise HTTPException(
            status_code=409,
            detail=f"{provider} is not connected — paste a key or connect it in Calendars & Sync",
        )
    return token


@app.post("/api/sync/linear")
async def sync_linear(body: LinearSyncIn, db: AsyncSession = Depends(session)) -> dict[str, int]:
    """Mirror Linear's started issues as schedulable tasks.

    Replace rather than merge, on a stable `linear:<issue-id>` key: an issue
    moved out of progress has to stop consuming time, and reusing the key keeps
    a re-sync from shuffling everything that did not change.
    """
    api_key = await _resolve_credential(db, "linear", body.api_key)
    try:
        issues = await fetch_linear_issues(api_key)
    except LinearError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    await db.execute(delete(IntentRow).where(IntentRow.id.startswith(LINEAR_PREFIX)))
    base = origin()
    for issue in issues:
        label = f"{issue.identifier} {issue.title}".strip()
        wire = IntentIn(
            title=label[:200],
            kind=IntentKind.TASK,
            priority=body.priority,
            minutes_per_period=issue.minutes,
            # An issue smaller than the default 30-minute floor would be
            # rejected by the domain model for demanding less than one chunk.
            min_chunk_minutes=min(30, issue.minutes),
            max_chunk_minutes=max(min(body.max_chunk_minutes, issue.minutes), issue.minutes),
        )
        intent = wire.to_domain(f"{LINEAR_PREFIX}{issue.id}", base)
        db.add(IntentRow(id=intent.id, payload=intent.model_dump(mode="json")))
    await db.commit()
    plan = await _replan(db)
    return {"issues": len(issues), "blocks": len(plan.blocks)}


class TokenSyncIn(BaseModel):
    token: str = Field(default="", max_length=500)


async def _sync_tasks(
    db: AsyncSession, prefix: str, kind_label: str, tasks: list[tuple[str, str, Priority, int]]
) -> int:
    """Store a provider's tasks as intents under a stable id prefix.

    Shared by Todoist and GitHub: both reduce to (id, title, priority,
    minutes) tuples, so the storage half — replace-by-prefix, `to_domain`,
    commit — does not need writing twice.
    """
    await db.execute(delete(IntentRow).where(IntentRow.id.startswith(prefix)))
    base = origin()
    for task_id, title, priority, minutes in tasks:
        wire = IntentIn(
            title=f"{kind_label}: {title}"[:200],
            kind=IntentKind.TASK,
            priority=priority,
            minutes_per_period=minutes,
            min_chunk_minutes=min(30, minutes),
            max_chunk_minutes=minutes,
        )
        intent = wire.to_domain(f"{prefix}{task_id}", base)
        db.add(IntentRow(id=intent.id, payload=intent.model_dump(mode="json")))
    await db.commit()
    return len(tasks)


@app.post("/api/sync/todoist")
async def sync_todoist(body: TokenSyncIn, db: AsyncSession = Depends(session)) -> dict[str, int]:
    """Mirror uncompleted Todoist tasks as schedulable intents."""
    token = await _resolve_credential(db, "todoist", body.token)
    try:
        tasks = await fetch_todoist_tasks(token)
    except TodoistError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    count = await _sync_tasks(
        db,
        TODOIST_PREFIX,
        "Todoist",
        [(t.id, t.content, t.priority, t.minutes) for t in tasks],
    )
    plan = await _replan(db)
    return {"tasks": count, "blocks": len(plan.blocks)}


@app.post("/api/sync/github")
async def sync_github(body: TokenSyncIn, db: AsyncSession = Depends(session)) -> dict[str, int]:
    """Mirror assigned open GitHub issues as schedulable intents."""
    token = await _resolve_credential(db, "github", body.token)
    try:
        issues = await fetch_github_issues(token)
    except GithubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    count = await _sync_tasks(
        db,
        GITHUB_PREFIX,
        "GitHub",
        [(i.id, f"#{i.number} {i.title}", Priority.P2, i.minutes) for i in issues],
    )
    plan = await _replan(db)
    return {"issues": count, "blocks": len(plan.blocks)}


async def _require_pasted_credential(body: TokenSyncIn, provider: str) -> str:
    """These three trackers have no OAuth app to fall back to (see
    integrations/{notion,clickup,jira}.py's docstrings for why) — a pasted
    credential is the only path, so its absence is the caller's mistake to
    fix, not a 409 "go connect it" that implies a button that doesn't exist.

    Stripped for the same reason `_resolve_credential` strips its own pasted
    key — a trailing newline from a copy-paste turns into an HTTP header
    httpx refuses to send, well before the request reaches the provider.
    """
    token = body.token.strip()
    if not token:
        raise HTTPException(status_code=422, detail=f"paste a {provider} credential first")
    return token


@app.post("/api/sync/notion")
async def sync_notion(body: TokenSyncIn, db: AsyncSession = Depends(session)) -> dict[str, int]:
    """Mirror every page in a Notion database as schedulable intents."""
    credential = await _require_pasted_credential(body, "Notion database_id:integration_token")
    try:
        tasks = await fetch_notion_tasks(credential)
    except NotionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    count = await _sync_tasks(
        db, NOTION_PREFIX, "Notion", [(t.id, t.title, Priority.P3, t.minutes) for t in tasks]
    )
    plan = await _replan(db)
    return {"tasks": count, "blocks": len(plan.blocks)}


@app.post("/api/sync/clickup")
async def sync_clickup(body: TokenSyncIn, db: AsyncSession = Depends(session)) -> dict[str, int]:
    """Mirror open ClickUp tasks assigned to the token owner as schedulable intents."""
    credential = await _require_pasted_credential(body, "ClickUp team_id:api_token")
    try:
        tasks = await fetch_clickup_tasks(credential)
    except ClickUpError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    count = await _sync_tasks(
        db,
        CLICKUP_PREFIX,
        "ClickUp",
        [(t.id, t.name, t.priority, t.minutes) for t in tasks],
    )
    plan = await _replan(db)
    return {"tasks": count, "blocks": len(plan.blocks)}


@app.post("/api/sync/jira")
async def sync_jira(body: TokenSyncIn, db: AsyncSession = Depends(session)) -> dict[str, int]:
    """Mirror unresolved Jira issues assigned to the token owner as schedulable intents."""
    credential = await _require_pasted_credential(body, "Jira site:email:api_token")
    try:
        issues = await fetch_jira_issues(credential)
    except JiraError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    count = await _sync_tasks(
        db,
        JIRA_PREFIX,
        "Jira",
        [(i.id, f"{i.key} {i.summary}".strip(), i.priority, i.minutes) for i in issues],
    )
    plan = await _replan(db)
    return {"issues": count, "blocks": len(plan.blocks)}


@app.post("/api/sync/google")
async def sync_google(db: AsyncSession = Depends(session)) -> dict[str, int]:
    """Mirror real Google Calendar events for the connected account.

    No body: the access token lives server-side (`/api/auth/google` put it
    there), never in a request from the browser.
    """
    token = await _resolve_credential(db, "google", "")
    provider = GoogleCalendarProvider(token, settings().zone)
    return await _mirror(db, provider, "google")


@app.post("/api/sync/outlook")
async def sync_outlook(db: AsyncSession = Depends(session)) -> dict[str, int]:
    """Mirror real Outlook / Microsoft 365 events for the connected account."""
    token = await _resolve_credential(db, "outlook", "")
    provider = OutlookCalendarProvider(token, settings().zone)
    return await _mirror(db, provider, "outlook")


class CalendarPushIn(BaseModel):
    provider: str


@app.post("/api/calendar/push")
async def push_calendar(
    body: CalendarPushIn, db: AsyncSession = Depends(session)
) -> dict[str, int]:
    """Push the current plan onto the connected provider's "Horolog" calendar
    right now, rather than waiting for the next background tick — the
    explicit "sync now" a user reaches for after enabling write-back."""
    if body.provider not in oauth.CALENDAR_PROVIDERS:
        raise HTTPException(status_code=404, detail=f"unknown provider {body.provider!r}")
    if not settings().calendar_writeback_enabled:
        raise HTTPException(
            status_code=409,
            detail="calendar write-back is disabled — set HOROLOG_CALENDAR_WRITEBACK_ENABLED=true",
        )
    return await _push_calendar(db, body.provider)


# --------------------------------------------------------------------------
# OAuth connections
# --------------------------------------------------------------------------


@app.get("/api/connections")
async def list_connections(db: AsyncSession = Depends(session)) -> dict[str, bool]:
    """Which providers have a stored, usable token — what the Connect page
    renders as "Connected" instead of guessing from browser state."""
    connected = await oauth.connected_providers(db)
    return {provider: provider in connected for provider in oauth.PROVIDERS}


@app.delete("/api/connections/{provider}")
async def disconnect(provider: str, db: AsyncSession = Depends(session)) -> Response:
    if provider not in oauth.PROVIDERS:
        raise HTTPException(status_code=404, detail=f"unknown provider {provider!r}")
    await oauth.forget_token(db, provider)
    return Response(status_code=204)


def _connect_redirect(web: str, **params: str) -> RedirectResponse:
    """A redirect to the Connect page's status banner.

    `error` in particular carries text from outside the process — the OAuth
    provider's own callback query string, which anyone can hit directly with
    any value they like, not only a real provider. Building the URL with an
    f-string let that value break out of the query string; `urlencode` is what
    keeps it confined to the `error` parameter's value.
    """
    return RedirectResponse(url=f"{web}/connect?{urlencode(params)}")


@app.get("/api/auth/{provider}")
async def auth_redirect(provider: str) -> RedirectResponse:
    cfg = settings()
    if provider not in oauth.PROVIDERS:
        raise HTTPException(status_code=404, detail=f"unknown provider {provider!r}")

    client_id, client_secret = oauth.client_credentials(cfg, provider)
    if not client_id or not client_secret:
        return _connect_redirect(
            cfg.public_web_url, status="credentials_missing", provider=provider
        )
    state = oauth.new_state()
    return RedirectResponse(url=oauth.authorize_url(cfg, provider, state))


@app.get("/api/auth/callback/{provider}")
async def auth_callback(
    provider: str,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(session),
) -> RedirectResponse:
    cfg = settings()
    web = cfg.public_web_url
    if provider not in oauth.PROVIDERS:
        raise HTTPException(status_code=404, detail=f"unknown provider {provider!r}")
    if error:
        return _connect_redirect(web, status="error", error=error)
    if not code or not state or not oauth.consume_state(state):
        # A missing or already-used state is what a replayed or forged
        # callback looks like — reject it rather than trust a bare code.
        return _connect_redirect(
            web, status="error", error="Invalid or expired authorization request"
        )

    try:
        token = await oauth.exchange_code(cfg, provider, code)
    except oauth.OAuthError as exc:
        return _connect_redirect(web, status="error", error=str(exc))

    await oauth.save_token(db, provider, token)
    # The token stays server-side — never appended here, never handed to the
    # browser. The frontend learns the connection succeeded and asks the
    # relevant /api/sync/* endpoint to use it.
    return _connect_redirect(web, status="success", provider=provider)


# --------------------------------------------------------------------------
# Booking links
# --------------------------------------------------------------------------

MIN_BOOKING_MINUTES = 15
MAX_BOOKING_MINUTES = 8 * 60


class FreeSlot(BaseModel):
    start: datetime
    end: datetime


@app.get("/api/availability")
async def availability(
    minutes: int = 30,
    days: int = 7,
    db: AsyncSession = Depends(session),
    mode: Literal["bookable", "idle"] = "bookable",
) -> list[FreeSlot]:
    """Openings a guest may book, inside the configured working window.

    "True free time": only real commitments close a slot. Horolog's own blocks
    are movable by construction, so offering an hour that currently holds focus
    time is not a double-booking — accepting it pushes that focus time
    elsewhere. Hiding those hours would hand a booking link a calendar that
    looks full while the day is actually open, which is the exact failure the
    scheduler exists to prevent.
    """
    if not MIN_BOOKING_MINUTES <= minutes <= MAX_BOOKING_MINUTES:
        raise HTTPException(
            status_code=422,
            detail=f"minutes must be between {MIN_BOOKING_MINUTES} and {MAX_BOOKING_MINUTES}",
        )
    cfg = settings()
    if not 1 <= days <= cfg.horizon_days:
        raise HTTPException(
            status_code=422, detail=f"days must be between 1 and {cfg.horizon_days}"
        )

    base = origin()
    need = minutes_to_slots(minutes)
    taken = bytearray(days * SLOTS_PER_DAY)
    for event in merge_busy(await _busy(db)):
        for slot in range(max(event.start_slot, 0), min(event.end_slot, len(taken))):
            taken[slot] = 1

    if mode == "idle":
        plan = await load_previous_plan(db) or await _replan(db)
        for block in plan.blocks:
            for slot in range(max(block.start_slot, 0), min(block.end_slot, len(taken))):
                taken[slot] = 1

    # Nothing in the past: `origin` is midnight, so most of today is behind us.
    # `to_slot` floors to the slot containing the current instant. That slot's
    # start is already in the past (except for an impossible zero-microsecond
    # race), so availability must begin at the following boundary.
    now_slot = to_slot(datetime.now(cfg.zone), base) + 1
    window = (cfg.workday_start_min // SLOT_MINUTES, cfg.workday_end_min // SLOT_MINUTES)
    out: list[FreeSlot] = []
    for day in range(days):
        day_lo = day * SLOTS_PER_DAY
        # Step by the meeting length so the offered slots tile the day rather
        # than overlap — two adjacent offers that cannot both be taken are a
        # booking page that contradicts itself.
        for start in range(day_lo + window[0], day_lo + window[1] - need + 1, need):
            if start < now_slot or any(taken[start : start + need]):
                continue
            out.append(FreeSlot(start=from_slot(start, base), end=from_slot(start + need, base)))
    return out


class BookIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(default="", max_length=200)
    start: LocalDateTime
    minutes: int = 30


class _RateLimiter:
    """A fixed window per key. Everything else here has no auth by design —
    a self-hosted single-user tool has no accounts to protect — but a booking
    link is deliberately the one URL meant to be handed to strangers, and
    unlike the rest of the API it writes to the calendar on every call. That
    combination is worth a floor.

    ponytail: in-process, unbounded by IP count. Fine for what a booking page
    actually receives; move to a real store if this ever runs multi-worker.
    """

    def __init__(self, limit: int, window_s: float) -> None:
        self._limit = limit
        self._window_s = window_s
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self._window_s
        recent = [t for t in self._hits.get(key, []) if t > cutoff]
        if len(recent) >= self._limit:
            self._hits[key] = recent
            return False
        recent.append(now)
        self._hits[key] = recent
        return True


_booking_limiter = _RateLimiter(limit=5, window_s=600)
"""5 bookings per IP per 10 minutes — enough for a real guest picking a slot,
retrying after a race, and correcting a typo; not enough to fill a calendar."""


@app.post("/api/book", status_code=201)
async def book(
    body: BookIn, request: Request, db: AsyncSession = Depends(session)
) -> dict[str, Any]:
    """Accept a booking from a shared link.

    The booking lands in the busy mirror, not the intent list, because a guest's
    commitment is exactly as immovable as any other real meeting — that is what
    makes the surrounding flexible work reschedule around it instead of the
    other way round.
    """
    client_ip = request.client.host if request.client else "unknown"
    if not _booking_limiter.allow(client_ip):
        raise HTTPException(status_code=429, detail="too many booking attempts, try again shortly")
    if not MIN_BOOKING_MINUTES <= body.minutes <= MAX_BOOKING_MINUTES:
        raise HTTPException(status_code=422, detail="unsupported meeting length")

    base = origin()
    start = to_slot(body.start, base)
    end = start + minutes_to_slots(body.minutes)
    if start < to_slot(datetime.now(settings().zone), base):
        raise HTTPException(status_code=409, detail="that time has already passed")
    if end > horizon_slots():
        raise HTTPException(
            status_code=422,
            detail=f"bookings only go {settings().horizon_days} days out",
        )
    for event in merge_busy(await _busy(db)):
        if start < event.end_slot and event.start_slot < end:
            raise HTTPException(
                status_code=409, detail="that slot was taken while you were choosing"
            )

    who = f"{body.name} <{body.email}>" if body.email else body.name
    db.add(
        BusyRow(
            id=f"booking:{uuid.uuid4().hex[:12]}",
            source="booking",
            label=f"Booked: {who}"[:200],
            start_slot=start,
            end_slot=end,
        )
    )
    await db.commit()
    plan = await _replan(db)
    return {
        "start": from_slot(start, base).isoformat(),
        "end": from_slot(end, base).isoformat(),
        "rescheduled_blocks": sum(1 for b in plan.blocks if b.moved_from is not None),
    }


@app.get("/api/plan.ics")
async def export_ics(db: AsyncSession = Depends(session)) -> Response:
    """Subscribe to the plan from any calendar app, read-only.

    The safest possible write path: a subscribing client can render the plan but
    can never corrupt the calendar it was derived from.
    """
    base = origin()
    plan = await load_previous_plan(db) or await _replan(db)
    titles = {i.id: i.title for i in await load_intents(db)}
    body = to_ics(
        [
            (
                titles.get(b.intent_id, b.intent_id),
                from_slot(b.start_slot, base),
                from_slot(b.end_slot, base),
            )
            for b in plan.blocks
        ]
    )
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={"content-disposition": 'attachment; filename="horolog.ics"'},
    )


@app.get("/api/stream")
async def stream() -> EventSourceResponse:
    async def events() -> AsyncIterator[dict[str, str]]:
        async with bus.subscribe() as queue:
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=20)
                except TimeoutError:
                    # Proxies drop idle connections; a comment frame keeps the
                    # stream alive without inventing a fake domain event.
                    yield {"event": "ping", "data": "{}"}
                    continue
                yield {"event": "plan", "data": payload}

    return EventSourceResponse(events())


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


async def _sync_connected_calendars(db: AsyncSession) -> None:
    """Re-mirror every connected Google/Outlook account.

    Same `_mirror()` path a manual Sync click uses, so this behaves identically
    from the frontend's perspective (SSE broadcast included) — the only
    difference is what triggers it. One provider failing must never stop the
    other or the next tick, so failures are caught here rather than raised.

    Deliberately broad: `_mirror` only ever *means* to raise `HTTPException`
    (it wraps every `SyncError` from the provider), but an upstream API
    returning a malformed body raises `json.JSONDecodeError` before it ever
    becomes a `SyncError` — narrower than `Exception` and this loop silently
    dies on the first bad response and never recovers, with no request/response
    cycle left afterwards for anyone to notice from. A scheduled tick must
    survive whatever a single provider does to it.
    """
    connected = await oauth.connected_providers(db)
    for provider_name in oauth.CALENDAR_PROVIDERS:
        if provider_name not in connected:
            continue
        token = await oauth.valid_access_token(db, settings(), provider_name)
        if not token:
            continue
        provider: CalendarProvider = (
            GoogleCalendarProvider(token, settings().zone)
            if provider_name == "google"
            else OutlookCalendarProvider(token, settings().zone)
        )
        try:
            await _mirror(db, provider, provider_name)
        except Exception as exc:  # see docstring: a scheduled tick must never kill the loop
            print(f"background sync: {provider_name} failed: {exc}", file=sys.stderr)


async def _push_connected_calendars(db: AsyncSession) -> None:
    """Push the plan onto every connected calendar, when write-back is on.

    Same resilience shape as `_sync_connected_calendars`: one provider's
    failure (an expired token, an unreachable API) must not stop the other or
    the next tick.
    """
    if not settings().calendar_writeback_enabled:
        return
    connected = await oauth.connected_providers(db)
    for provider_name in oauth.CALENDAR_PROVIDERS:
        if provider_name not in connected:
            continue
        try:
            await _push_calendar(db, provider_name)
        except Exception as exc:  # a scheduled tick must never kill the loop
            print(f"background push: {provider_name} failed: {exc}", file=sys.stderr)


async def _sync_loop() -> None:
    """Background heartbeat started from `lifespan()`.

    A fresh session per tick, not a request-scoped one — nothing here runs
    inside a request. `asynccontextmanager` turns the same `session()`
    generator the HTTP layer uses via `Depends` into a plain context manager,
    so this needs no new session-creation code in `db.py`.
    """
    interval_s = settings().sync_interval_minutes * 60
    while True:
        await asyncio.sleep(interval_s)
        async with asynccontextmanager(session)() as db:
            await _sync_connected_calendars(db)
            await _push_connected_calendars(db)


async def _mirror(db: AsyncSession, provider: CalendarProvider, source: str) -> dict[str, int]:
    """Replace the mirror for one source, then re-plan.

    Replace rather than merge: the mirror is meant to be an exact reflection of
    the upstream calendar, so a meeting deleted there has to free its slot here.
    Scoped to `source` so syncing an ICS feed does not wipe CalDAV events.
    """
    try:
        events = await provider.fetch(origin(), settings().horizon_days)
    except SyncError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    await db.execute(delete(BusyRow).where(BusyRow.source == source))
    for event in events:
        db.add(
            BusyRow(
                id=f"{source}:{uuid.uuid4().hex[:12]}",
                source=source,
                label=event.label,
                start_slot=event.start_slot,
                end_slot=event.end_slot,
            )
        )
    await db.commit()
    plan = await _replan(db)
    return {"events": len(events), "blocks": len(plan.blocks)}


async def _push_calendar(db: AsyncSession, provider_name: str) -> dict[str, int]:
    """Diff the current plan against `synced_blocks` for one provider and
    apply only what changed on that provider's dedicated "Horolog" calendar —
    create, move, or remove one event per block that actually differs.

    This is the write-side payoff of two-pass placement: the same property
    that limits a re-solve to moving only the blocks actually hit limits a
    push to one API call per block actually hit, not a wipe-and-recreate of
    the whole calendar.
    """
    token = await oauth.valid_access_token(db, settings(), provider_name)
    if not token:
        raise HTTPException(status_code=409, detail=f"{provider_name} is not connected")

    writer: GoogleCalendarWriter | OutlookCalendarWriter = (
        GoogleCalendarWriter(token) if provider_name == "google" else OutlookCalendarWriter(token)
    )

    plan = await load_previous_plan(db) or await _replan(db)
    base = origin()
    titles = {i.id: i for i in await load_intents(db, base)}
    current: dict[tuple[str, int, int], tuple[int, int, str]] = {
        b.key: (
            b.start_slot,
            b.end_slot,
            titles[b.intent_id].title if b.intent_id in titles else b.intent_id,
        )
        for b in plan.blocks
    }

    rows = list(
        (await db.execute(select(SyncedBlockRow).where(SyncedBlockRow.provider == provider_name)))
        .scalars()
        .all()
    )

    created = moved = removed = 0
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            calendar_id = await writer.ensure_calendar(client)
        except SyncError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        # Every stored row points at the calendar that existed on the last
        # push. If that no longer matches, the user deleted the Horolog
        # calendar in the meantime and `ensure_calendar` just made a fresh
        # one — every `event_id` below is dead. Forget them rather than
        # silently treating an unmoved slot as "already written".
        if rows and rows[0].calendar_id != calendar_id:
            for row in rows:
                await db.delete(row)
            rows = []

        synced = {(r.intent_id, r.occurrence, r.chunk): r for r in rows}
        creates = [key for key in current if key not in synced]
        updates = [
            key
            for key in current
            if key in synced and (synced[key].start_slot, synced[key].end_slot) != current[key][:2]
        ]
        deletes = [key for key in synced if key not in current]

        ops = creates + updates + deletes
        if len(ops) > MAX_PUSH_OPS:
            keep = set(ops[:MAX_PUSH_OPS])
            creates = [k for k in creates if k in keep]
            updates = [k for k in updates if k in keep]
            deletes = [k for k in deletes if k in keep]

        for key in creates:
            intent_id, occurrence, chunk = key
            start_slot, end_slot, title = current[key]
            try:
                event_id = await writer.create_event(
                    client,
                    calendar_id,
                    title,
                    from_slot(start_slot, base),
                    from_slot(end_slot, base),
                )
            except SyncError as exc:
                logger.warning(
                    "push %s: could not create event for %r: %s", provider_name, title, exc
                )
                continue
            db.add(
                SyncedBlockRow(
                    provider=provider_name,
                    intent_id=intent_id,
                    occurrence=occurrence,
                    chunk=chunk,
                    calendar_id=calendar_id,
                    event_id=event_id,
                    start_slot=start_slot,
                    end_slot=end_slot,
                )
            )
            created += 1

        for key in updates:
            row = synced[key]
            start_slot, end_slot, _ = current[key]
            try:
                await writer.patch_event(
                    client,
                    calendar_id,
                    row.event_id,
                    from_slot(start_slot, base),
                    from_slot(end_slot, base),
                )
            except SyncError as exc:
                logger.warning(
                    "push %s: could not move event %s: %s", provider_name, row.event_id, exc
                )
                continue
            row.start_slot, row.end_slot = start_slot, end_slot
            moved += 1

        for key in deletes:
            row = synced[key]
            try:
                await writer.delete_event(client, calendar_id, row.event_id)
            except SyncError as exc:
                logger.warning(
                    "push %s: could not remove event %s: %s", provider_name, row.event_id, exc
                )
                continue
            await db.delete(row)
            removed += 1

    await db.commit()
    return {"created": created, "moved": moved, "removed": removed}


async def _busy(db: AsyncSession) -> list[BusyInterval]:
    """Every interval the calendar is already spoken for.

    The single place the mirror is read, so the solver, the analytics page, the
    booking page and the planner grid can never disagree about what is occupied.
    Decompression buffers are derived here rather than stored: they are a
    function of the meetings, and persisting them would leave orphans behind
    every time a meeting moved.
    """
    rows = (await db.execute(select(BusyRow))).scalars().all()
    busy = [
        BusyInterval(
            source_id=row.id, start_slot=row.start_slot, end_slot=row.end_slot, label=row.label
        )
        for row in rows
    ]
    # Convert absolute commitments at read time so a date does not drift at midnight.
    base = origin()
    fixed = (await db.execute(select(CalendarBlockRow))).scalars().all()
    for row in fixed:
        start, end = _calendar_block_bounds(row)
        end_slot = to_slot(end, base, round_up=True)
        span = _clip(to_slot(start, base), end_slot)
        if span is not None:
            busy.append(
                BusyInterval(
                    source_id="calendar-block:" + row.id,
                    label=row.title,
                    start_slot=span[0],
                    end_slot=span[1],
                )
            )
    cfg = settings()
    if cfg.auto_buffer_enabled:
        busy += decompression_buffers(busy, cfg.auto_buffer_minutes)
    return busy


async def _replan(db: AsyncSession) -> Plan:
    base = origin()
    intents = await load_intents(db, base)
    plan = solve(
        intents,
        await _busy(db),
        horizon_slots(),
        previous=await load_previous_plan(db),
        start_weekday=base.weekday(),
        not_before_slot=max(0, to_slot(datetime.now(settings().zone), base, round_up=True)),
    )
    await save_plan(db, plan)
    bus.publish(json.dumps({"blocks": len(plan.blocks), "solve_ms": round(plan.solve_ms, 2)}))
    return plan


def _deadline(intent: Intent | None, base: datetime) -> datetime | None:
    return (
        from_slot(intent.due_slot, base)
        if intent is not None and intent.due_slot is not None
        else None
    )


def _lateness(intent: Intent | None, end_slot: int) -> int:
    return (
        max(0, end_slot - intent.due_slot) * SLOT_MINUTES
        if intent is not None and intent.due_slot is not None
        else 0
    )


async def _render(db: AsyncSession, plan: Plan) -> PlanOut:
    base = origin()
    titles = {i.id: i for i in await load_intents(db, base)}
    rows = (await db.execute(select(BusyRow))).scalars().all()
    sources = {row.id: row.source for row in rows}
    return PlanOut(
        blocks=[
            BlockOut(
                intent_id=b.intent_id,
                title=titles[b.intent_id].title if b.intent_id in titles else b.intent_id,
                color=titles[b.intent_id].color if b.intent_id in titles else None,
                tags=titles[b.intent_id].tags if b.intent_id in titles else [],
                kind=titles[b.intent_id].kind if b.intent_id in titles else IntentKind.TASK,
                priority=b.priority,
                energy=titles[b.intent_id].energy if b.intent_id in titles else None,
                occurrence=b.occurrence,
                chunk=b.chunk,
                start=from_slot(b.start_slot, base),
                end=from_slot(b.end_slot, base),
                moved_from=from_slot(b.moved_from, base) if b.moved_from is not None else None,
                deadline=_deadline(titles.get(b.intent_id), base),
                late_by_minutes=_lateness(titles.get(b.intent_id), b.end_slot),
                completed=(
                    from_slot(b.start_slot, base).date() in titles[b.intent_id].completed_dates
                    if b.intent_id in titles else False
                ),
                recurring=(
                    titles[b.intent_id].period_days is not None
                    if b.intent_id in titles else False
                ),
            )
            for b in plan.blocks
        ],
        unmet=[
            UnmetOut(
                intent_id=u.intent_id,
                title=titles[u.intent_id].title if u.intent_id in titles else u.intent_id,
                priority=u.priority,
                occurrence=u.occurrence,
                shortfall_minutes=u.shortfall_slots * 15,
            )
            for u in plan.unmet
        ],
        busy=[
            BusyIn(
                label=event.label,
                # Derived buffers have no row of their own; the grid still has
                # to show them, or time disappears with no visible reason.
                source=(
                    "calendar-block"
                    if event.source_id.startswith("calendar-block:")
                    else sources.get(event.source_id, BUFFER_SOURCE)
                ),
                start=from_slot(event.start_slot, base),
                end=from_slot(event.end_slot, base),
            )
            for event in await _busy(db)
        ],
        solve_ms=plan.solve_ms,
        complete=plan.complete,
        generated_at=datetime.now(UTC),
        origin=base,
        horizon_days=settings().horizon_days,
    )


__all__ = ["app", "minutes_to_slots", "timedelta"]
