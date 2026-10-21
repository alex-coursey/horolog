# ChatGPT MCP integration

The bridge in `/opt/horolog-mcp/server.py` manages tasks and fixed commitments
through the existing Horolog API and Authentik connection. It exposes task
listing, retrieval, creation, editing, deletion, completion, and uncompletion;
calendar-block creation, listing, editing, and deletion; skipping/restoring fixed
weekly sessions; availability; the schedule; and a schedule summary.

## Task edits and dates

`update_task(task_id, changes)` sends PATCH with only supplied fields. It validates
the merged complete Intent. Unspecified windows, bounds, attendee constraints,
completion, colors, tags, weekdays and Zoom fields survive. Server-owned fields
are rejected. The web inbox also uses partial updates.

`tags` replaces the list; `tags: []` clears it. `add_tags` and `remove_tags` run
in that order after replacement. Null color restores the priority default.
Supported colors are p1–p4, slate, blue, green, red, yellow, purple, pink, orange
and #RRGGBB. Title/color/tag edits skip replanning. Custom tags can be filtered
in the planner.

PATCH accepts IntentIn fields plus daily_windows, add_tags, remove_tags and
session_minutes. daily_windows replaces all windows ([] allows the whole day).
window_start_min/window_end_min edit the first window and cannot be combined
with daily_windows. Null due/earliest clears the corresponding bound.
Datetime strings with offsets are interpreted in Horolog's configured timezone;
floating strings use that timezone directly. The solver uses 15-minute wall-clock
slots. Earliest starts round up; attendee busy ranges round outwards.

Intent slot_origin anchors due/earliest and attendee constraints to calendar
dates. Reads and solves rebase offsets against today's local midnight, including
across daylight-saving changes. On startup, legacy rows without an origin are
anchored once to the dates currently displayed. Their original historical dates
cannot be recovered reliably from offsets alone; this does not attempt to guess
or retroactively correct them. GET/list return offsets relative to their returned
slot_origin. Existing PUT replacement semantics remain available.

New or displaced placements cannot start in elapsed time. Already placed blocks
that have started remain stable if their duration and constraints still permit
that placement. Cached plans expire at local midnight.

Deadlines remain soft targets. Plan blocks include deadline and late_by_minutes;
the planner marks late blocks, and get_schedule_summary returns late_blocks as
well as unmet demand. A complete plan means all demand was placed, not that every
deadline was met. Requirements with no legal windows appear as unmet. Existing
recurrence semantics emit whole periods only; a partial trailing period does not
demand a full quota.

## Habits and single-session exceptions

Weekdays use Python numbering: Monday=0, Sunday=6. minutes_per_period is total
demand. For 45-minute sessions on Monday and Sunday: period_days=7,
allowed_weekdays=[0,6], minutes_per_period=90, min_chunk_minutes=45,
max_chunk_minutes=45, max_per_day=1. MCP create_task takes duration_minutes as
per-session length when weekdays are supplied.

For this fixed weekly shape, editing weekdays alone preserves session length and
recalculates total demand. session_minutes edits session length and recalculates
both chunk bounds and total demand. Do not combine that shorthand with explicit
demand/chunk overrides. Supplying minutes_per_period explicitly retains control
over total demand. General weekly time budgets are not automatically converted.

`skip_task_session(task_id, session_date)` excludes that local date and deducts
one session from that period's quota, without making the user do it elsewhere.
The date must be today/future and on an allowed weekday. Repeating a skip is safe;
restore_task_session undoes it. This is supported only for weekly fixed sessions
with one session per allowed weekday. General time budgets receive a clear 422.
A recurrence shape change requires restoring exceptions first if the new shape
would not support them. Completion remains one-time only; skips do not complete
habits or implement per-occurrence completion/streaks.

## Fixed events and absences

`create_calendar_block(title, all_day_date="2026-10-27")` blocks midnight through
midnight the next day in Horolog's local timezone. Optional end_date is exclusive
for multi-day absences. For a timed appointment, supply ISO start/end instead.
Example: title="College final — San Diego", all_day_date="2026-10-27".

Tasks cannot overlap a block; they move to other allowed times or appear as
unmet demand. A date block preserves weekly quotas; use session skipping when
the intent is to cancel a session without making it up.

list_calendar_blocks returns IDs. update_calendar_block partially edits a block;
moving all_day_date alone preserves its number of days. Supply both start/end
when moving a timed appointment to another date. delete_calendar_block removes
only that block and replans. Creation and editing return the resulting plan so
ChatGPT can report conflicts/unmet demand.

Absolute UTC bounds live in calendar_blocks and are converted to local slots on
read. Timed ranges round outwards. Dates outside the current horizon remain stored
and affect solves when in range. Legacy PUT /api/busy still affects only its
existing sources and cannot erase these blocks. These are Horolog commitments;
they do not create events in Google or Outlook.

## Availability and retry safety

get_availability(mode="bookable") retains booking semantics: fixed commitments
close slots, while movable tasks may be shifted. mode="idle" excludes scheduled
tasks too. Use idle for requests for time with nothing scheduled. Both modes
respect working windows, date blocks and the current time.

For each intended create_task/create_calendar_block call, supply a unique
request_id and reuse it on retries. The bridge forwards it as Idempotency-Key.
The API stores a hash scoped to the resource type in creation_requests. Concurrent
requests with the same key and input return the same resource ID. Reusing a key
with different input, or after deleting that resource, returns 409. Keys are
retained; an intentional new identical item needs a new key. Requests without
keys retain ordinary create semantics; identical titles alone are not duplicates.

## Verification and deployment

API: ruff check, ruff format --check, mypy horolog, pytest. The optional Anthropic
SDK must be absent for the existing missing-SDK test. Frontend: npm run typecheck
and npm run build. Launcher: node --test scripts/dev-env.test.mjs.
Bridge offline tests: `.venv/bin/python -m unittest discover -s . -p test_tools.py -v`
from /opt/horolog-mcp; no OAuth credentials or production requests are needed.

PostgreSQL tests are opt-in via HOROLOG_TEST_POSTGRES_URL and must use a disposable
database. They cover persistence and concurrent retries, deleting only their
own records, without drops or resets. Ordinary API tests use temporary SQLite.

These additions require deployment approval. Startup creates missing
calendar_blocks/creation_requests tables and adds slot_origin metadata to legacy
Intent JSON while retaining currently displayed dates. No destructive migration,
OAuth, DNS, Authentik or tunnel change is needed. After approval, back up the DB,
build/replace only API and web, restart the MCP systemd service, refresh ChatGPT's
tool discovery, and perform read-only smoke checks. Do not use existing production
tasks for write tests.
