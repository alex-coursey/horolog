import os
from typing import Any, Literal
from urllib.parse import quote, urlsplit

from pydantic import Field, BaseModel
from mcp.server.auth.settings import AuthSettings
from auth_verifier import OIDCTokenVerifier
from mcp.server.transport_security import TransportSecuritySettings

import httpx
from mcp.server.fastmcp import FastMCP

HOROLOG_API_URL = os.getenv("HOROLOG_API_URL", "http://127.0.0.1:8000").rstrip("/")
MCP_HOST = os.getenv("HOROLOG_MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("HOROLOG_MCP_PORT", "8100"))
if not 1 <= MCP_PORT <= 65535:
    raise ValueError("HOROLOG_MCP_PORT must be between 1 and 65535")
MCP_RESOURCE_URL = os.environ["HOROLOG_MCP_RESOURCE_URL"].rstrip("/")
resource = urlsplit(MCP_RESOURCE_URL)
if (resource.scheme not in {"http", "https"} or not resource.netloc
        or resource.path != "/mcp" or resource.username or resource.password
        or resource.query or resource.fragment):
    raise ValueError("HOROLOG_MCP_RESOURCE_URL must be an absolute URL ending in /mcp")
if resource.scheme != "https" and resource.hostname not in {"localhost", "127.0.0.1"}:
    raise ValueError("A nonlocal MCP resource URL must use HTTPS")
resource_origin = f"{resource.scheme}://{resource.netloc}"
allowed_hosts = [resource.netloc, f"127.0.0.1:{MCP_PORT}", f"localhost:{MCP_PORT}"]
if MCP_HOST not in {"0.0.0.0", "::"}:
    allowed_hosts.append(f"{MCP_HOST}:{MCP_PORT}")

mcp = FastMCP(
    "Horolog",
    host=MCP_HOST,
    port=MCP_PORT,
    token_verifier=OIDCTokenVerifier(),
    auth=AuthSettings(
        issuer_url=os.environ["HOROLOG_OAUTH_ISSUER"],
        resource_server_url=MCP_RESOURCE_URL,
        validate_token_resource=False,
        required_scopes=[],
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=[resource_origin],
    ),
)

async def horolog_request(
    method: str,
    endpoint: str,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> Any:
    """Send a request to Horolog's REST API."""

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.request(
            method=method,
            url=f"{HOROLOG_API_URL}{endpoint}",
            json=payload,
            headers={"Idempotency-Key": idempotency_key} if idempotency_key is not None else None,
        )

        if response.is_error:
            try:
                detail = response.json().get("detail", "Request failed")
            except (ValueError, AttributeError):
                detail = "Request failed"
            raise ValueError(f"Horolog API {response.status_code}: {detail}")
        return response.json() if response.content else {"deleted": True}


@mcp.tool()
async def list_tasks() -> Any:
    """List all tasks, habits, and scheduling intents."""

    return await horolog_request("GET", "/api/intents")


@mcp.tool()
async def get_schedule() -> Any:
    """Get the current Horolog calendar plan."""

    return await horolog_request("GET", "/api/plan")


@mcp.tool()
async def create_task(
    title: str,
    duration_minutes: int,
    priority: int = 3,
    allowed_weekdays: list[int] | None = None,
    color: str | None = None,
    tags: list[str] | None = None,
    due: str | None = None,
    earliest: str | None = None,
    request_id: str | None = None,
) -> Any:
    """Create a task or recurring habit.

    allowed_weekdays uses Monday=0 through Sunday=6.
    For recurring habits, duration_minutes is the duration of each session.
    due/earliest accept ISO datetimes; deadlines remain soft scheduling targets.
    Supply a unique request_id per intended creation and reuse it for retries;
    this prevents duplicates without merging intentionally identical tasks.
    """
    if not title.strip():
        raise ValueError("Task title cannot be empty.")
    if duration_minutes <= 0:
        raise ValueError("Duration must be greater than zero.")
    if priority not in (1, 2, 3, 4):
        raise ValueError("Priority must be between 1 and 4.")

    payload: dict[str, Any] = {
        "title": title,
        "kind": "task",
        "minutes_per_period": duration_minutes,
        "priority": priority,
        "min_chunk_minutes": min(30, duration_minutes),
    }

    if color is not None:
        if not color.strip() or len(color) > 32:
            raise ValueError("Color must contain 1–32 characters.")
        payload["color"] = color

    if tags is not None:
        if any(not isinstance(tag, str) or not tag.strip() for tag in tags):
            raise ValueError("Tags must be nonempty strings.")
        payload["tags"] = tags

    if allowed_weekdays is not None:
        if not allowed_weekdays or len(set(allowed_weekdays)) != len(allowed_weekdays):
            raise ValueError("Weekdays must be nonempty and unique.")
        if any(type(day) is not int or day not in range(7) for day in allowed_weekdays):
            raise ValueError("Weekdays must be integers from 0 to 6.")

        payload.update({
            "kind": "habit",
            "period_days": 7,
            "minutes_per_period": duration_minutes * len(allowed_weekdays),
            "min_chunk_minutes": duration_minutes,
            "max_chunk_minutes": duration_minutes,
            "max_per_day": 1,
            "allowed_weekdays": allowed_weekdays,
        })

    if due is not None:
        payload["due"] = due
    if earliest is not None:
        payload["earliest"] = earliest
    if request_id is not None:
        return await horolog_request("POST", "/api/intents", payload, idempotency_key=request_id)
    return await horolog_request("POST", "/api/intents", payload)



class TaskChanges(BaseModel):
    """Only explicitly supplied fields are sent. Null clears nullable fields.

    minutes_per_period is total demand per period, not session duration.
    Weekly fixed sessions: set period_days=7, minutes_per_period=session*days,
    min_chunk_minutes=max_chunk_minutes=session, max_per_day=1, allowed_weekdays.
    tags replaces; add_tags/remove_tags modify it. Weekdays: Monday=0, Sunday=6.
    """
    model_config = {"extra": "forbid"}
    title: str | None = None
    minutes_per_period: int | None = None
    session_minutes: int | None = None
    priority: int | None = None
    kind: str | None = None
    period_days: int | None = None
    allowed_weekdays: list[int] | None = None
    min_chunk_minutes: int | None = None
    max_chunk_minutes: int | None = None
    max_per_day: int | None = None
    color: str | None = None
    tags: list[str] | None = None
    add_tags: list[str] = Field(default_factory=list)
    remove_tags: list[str] = Field(default_factory=list)
    due: str | None = None
    earliest: str | None = None
    window_start_min: int | None = None
    window_end_min: int | None = None
    daily_windows: list[dict[str, int]] | None = None
    preferred_start_min: int | None = None
    energy: str | None = None


def task_path(task_id: str) -> str:
    if not task_id.strip():
        raise ValueError("Task ID cannot be empty.")
    return f"/api/intents/{quote(task_id, safe='')}"


@mcp.tool()
async def get_task(task_id: str) -> Any:
    """Get one task or habit, including all scheduling constraints and completion."""
    return await horolog_request("GET", task_path(task_id))


@mcp.tool()
async def update_task(task_id: str, changes: TaskChanges) -> Any:
    """Partially edit a task. Omitted fields remain unchanged.

    Supply title, minutes_per_period (total per period), priority, recurrence,
    weekdays (Monday=0), chunk sizes, windows, deadlines, color or tags in changes.
    For weekly fixed sessions, changing weekdays alone preserves session length
    and adjusts total demand. session_minutes edits the per-session duration;
    do not combine it with minutes_per_period or chunk overrides.
    Null color restores priority defaults; null period_days makes a one-time task.
    tags=[] clears tags; add_tags/remove_tags edit tags without replacing the list.
    Dates use ISO 8601 with an offset, or Horolog's configured local timezone.
    """
    payload = changes.model_dump(exclude_unset=True)
    if not payload:
        raise ValueError("Provide at least one change.")
    return await horolog_request("PATCH", task_path(task_id), payload)


@mcp.tool()
async def delete_task(task_id: str) -> Any:
    """Permanently delete the specified task or habit."""
    return await horolog_request("DELETE", task_path(task_id))


@mcp.tool()
async def complete_task(task_id: str) -> Any:
    """Complete a one-time task. Recurring habits cannot be completed."""
    return await horolog_request("POST", task_path(task_id) + "/complete")


@mcp.tool()
async def uncomplete_task(task_id: str) -> Any:
    """Undo completion of a one-time task."""
    return await horolog_request("DELETE", task_path(task_id) + "/complete")


@mcp.tool()
async def get_availability(minutes: int = 30, days: int = 7, mode: Literal["bookable", "idle"] = "bookable") -> Any:
    """Find working-hour openings. mode='bookable' allows movable tasks to shift.

    mode='idle' excludes both fixed commitments and currently scheduled tasks.
    Use idle when the user asks for time with nothing scheduled.
    """
    if minutes < 1 or days < 1:
        raise ValueError("Minutes and days must be positive.")
    if mode not in {"bookable", "idle"}:
        raise ValueError("mode must be bookable or idle")
    endpoint = f"/api/availability?minutes={minutes}&days={days}"
    if mode == "idle":
        endpoint += "&mode=idle"
    return await horolog_request("GET", endpoint)


@mcp.tool()
async def get_schedule_summary() -> Any:
    """Summarize the current horizon, scheduled minutes and unmet demand.

    Unmet demand is returned explicitly, including shortfall minutes per task.
    Late blocks are reported separately: deadlines are soft targets, not guarantees.
    """
    from datetime import datetime

    plan = await get_schedule()
    by_task: dict[str, dict[str, Any]] = {}
    for block in plan["blocks"]:
        item = by_task.setdefault(block["intent_id"], {
            "task_id": block["intent_id"], "title": block["title"], "scheduled_minutes": 0,
        })
        item["scheduled_minutes"] += int((
            datetime.fromisoformat(block["end"]) - datetime.fromisoformat(block["start"])
        ).total_seconds() / 60)
    return {
        "origin": plan["origin"], "horizon_days": plan["horizon_days"],
        "complete": plan["complete"], "tasks": list(by_task.values()),
        "scheduled_minutes": sum(t["scheduled_minutes"] for t in by_task.values()),
        "unmet": plan["unmet"],
        "late_blocks": [b for b in plan["blocks"] if b.get("late_by_minutes", 0) > 0],
    }


@mcp.tool()
async def create_calendar_block(
    title: str,
    start: str | None = None,
    end: str | None = None,
    all_day_date: str | None = None,
    end_date: str | None = None,
    request_id: str | None = None,
) -> Any:
    """Add a fixed event or mark a date unavailable without replacing other events.

    All day: supply all_day_date='2030-01-15'. Dates use Horolog's configured
    local timezone. Optional end_date is exclusive, for multi-day absences.
    Timed event: supply ISO start and end (prefer offsets), omit all_day_date.
    Example: title='Conference day', all_day_date='2030-01-15'.
    Tasks cannot overlap the commitment; they move to other allowed times or
    appear as unmet demand. This does not delete tasks or reduce weekly quotas.
    Returns the created block and resulting plan, including unmet demand.
    Supply a unique request_id per intended creation and reuse it on retries.
    """
    payload: dict[str, Any] = {"title": title}
    for name, value in (("start", start), ("end", end),
                        ("all_day_date", all_day_date), ("end_date", end_date)):
        if value is not None:
            payload[name] = value
    if request_id is not None:
        return await horolog_request("POST", "/api/calendar-blocks", payload, idempotency_key=request_id)
    return await horolog_request("POST", "/api/calendar-blocks", payload)


@mcp.tool()
async def list_calendar_blocks() -> Any:
    """List fixed events and date blocks created through Horolog, including their IDs.

    Synced external calendar commitments remain visible through get_schedule.
    """
    return await horolog_request("GET", "/api/calendar-blocks")


@mcp.tool()
async def delete_calendar_block(block_id: str) -> Any:
    """Remove a Horolog fixed event/date block by ID and replan the freed time.

    Does not delete tasks, synced events, or other calendar blocks.
    """
    if not block_id.strip():
        raise ValueError("Block ID cannot be empty.")
    return await horolog_request("DELETE", f"/api/calendar-blocks/{quote(block_id, safe='')}")


class CalendarBlockChanges(BaseModel):
    model_config = {"extra": "forbid"}
    title: str | None = None
    start: str | None = None
    end: str | None = None
    all_day_date: str | None = None
    end_date: str | None = None


@mcp.tool()
async def update_calendar_block(block_id: str, changes: CalendarBlockChanges) -> Any:
    """Edit a fixed appointment/date block, preserving unspecified fields.

    Moving all_day_date alone preserves the number of days. Timed edits use ISO
    datetimes; supply both start and end to move a timed event to another date.
    Returns the resulting plan and unmet demand. Use list_calendar_blocks for IDs.
    """
    if not block_id.strip() or not changes.model_fields_set:
        raise ValueError("Provide a block ID and at least one change")
    return await horolog_request("PATCH", f"/api/calendar-blocks/{quote(block_id, safe='')}",
                                 changes.model_dump(exclude_unset=True))


@mcp.tool()
async def skip_task_session(task_id: str, session_date: str) -> Any:
    """Skip one fixed weekly habit session on YYYY-MM-DD without making it up.

    Reduces that period's demand by one session and prevents scheduling on that
    date. Supports weekly habits with fixed chunk size, one session per weekday.
    Other weekly goals get a clear unsupported-shape error. Does not complete or
    delete the habit. Repeating the same skip is safe. Dates use Horolog's zone.
    """
    return await horolog_request("POST", task_path(task_id) + "/skips/" + quote(session_date, safe=''))


@mcp.tool()
async def restore_task_session(task_id: str, session_date: str) -> Any:
    """Undo a skipped session on YYYY-MM-DD and restore that period's demand."""
    return await horolog_request("DELETE", task_path(task_id) + "/skips/" + quote(session_date, safe=''))


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
