"""End-to-end HTTP tests against an in-process app and a temp database."""

from __future__ import annotations

import os
import tempfile
import typing
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import cast

import httpx
import pytest
import pytest_asyncio

_tmpdir = tempfile.mkdtemp()
os.environ["HOROLOG_DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmpdir}/test.db"

from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from horolog.api import app, origin
from horolog.db import BusyRow, CalendarBlockRow, init_db, session


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        for path in ("/api/intents",):
            for item in (await http.get(path)).json():
                await http.delete(f"/api/intents/{item['id']}")
        await http.put("/api/busy", json=[])
        # `PUT /api/busy` with an empty body only clears the "manual"
        # source by design (each sync source is its own independent
        # mirror) — but `db.py`'s `_engine`/`settings` are process-wide
        # `@lru_cache`s, so when the full suite runs in one pytest process,
        # whichever test file's database is touched first is what every
        # other file's tests actually hit. A "booking"-sourced busy row
        # left behind by test_integrations.py's booking tests can land
        # here too, so every source needs clearing, not just "manual".
        gen = cast("AsyncGenerator[AsyncSession, None]", session())
        db = await anext(gen)
        await db.execute(delete(BusyRow))
        await db.execute(delete(CalendarBlockRow))
        await db.commit()
        await gen.aclose()
        yield http


@pytest.mark.asyncio
async def test_health(client: AsyncClient) -> None:
    assert (await client.get("/api/health")).json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_create_intent_schedules_it(client: AsyncClient) -> None:
    created = await client.post(
        "/api/intents",
        json={"title": "Write the design doc", "minutes_per_period": 120, "priority": 2},
    )
    assert created.status_code == 201

    plan = (await client.get("/api/plan")).json()
    mine = [b for b in plan["blocks"] if b["title"] == "Write the design doc"]
    assert mine, "a new intent must appear on the plan immediately"
    assert sum(1 for _ in mine) >= 1
    assert plan["complete"] is True
    assert plan["solve_ms"] < 500


@pytest.mark.asyncio
async def test_focus_intent_round_trips(client: AsyncClient) -> None:
    """The habits page used to mislabel every intent it created as a habit,
    even the ones its own presets called 'focus' - this pins the fix."""
    created = await client.post(
        "/api/intents",
        json={
            "title": "Deep work",
            "kind": "focus",
            "priority": 2,
            "energy": "high",
            "minutes_per_period": 600,
            "period_days": 7,
            "min_chunk_minutes": 120,
            "max_chunk_minutes": 120,
        },
    )
    assert created.status_code == 201

    intents = (await client.get("/api/intents")).json()
    mine = next(i for i in intents if i["title"] == "Deep work")
    assert mine["kind"] == "focus"
    assert mine["energy"] == "high"


@pytest.mark.asyncio
async def test_busy_events_push_work_aside(client: AsyncClient) -> None:
    await client.post(
        "/api/intents",
        json={
            "title": "Deep work",
            "minutes_per_period": 120,
            "min_chunk_minutes": 120,
            "max_chunk_minutes": 120,
        },
    )
    before = (await client.get("/api/plan")).json()
    first = next(b for b in before["blocks"] if b["title"] == "Deep work")

    base = origin()
    blocked = [
        {
            "label": "All-hands",
            "start": (base + timedelta(days=d, hours=9)).isoformat(),
            "end": (base + timedelta(days=d, hours=17)).isoformat(),
        }
        for d in range(3)
    ]
    await client.put("/api/busy", json=blocked)

    after = (await client.get("/api/plan")).json()
    moved = next(b for b in after["blocks"] if b["title"] == "Deep work")
    assert moved["start"] != first["start"], "work must move off a newly blocked day"
    for block in after["blocks"]:
        assert not (block["start"] < blocked[0]["end"] and blocked[0]["start"] < block["end"])


@pytest.mark.asyncio
async def test_replanning_an_unchanged_calendar_changes_nothing(client: AsyncClient) -> None:
    for i in range(4):
        await client.post("/api/intents", json={"title": f"task {i}", "minutes_per_period": 60})

    first = (await client.post("/api/plan/solve")).json()
    again = (await client.post("/api/plan/solve")).json()

    def key(plan: dict[str, typing.Any]) -> dict[str, str]:
        return {
            f"{b['intent_id']}:{b['occurrence']}:{b['chunk']}": b["start"] for b in plan["blocks"]
        }

    assert key(first) == key(again), "a no-op re-solve must not move a single block"


@pytest.mark.asyncio
async def test_impossible_intent_is_rejected_with_a_reason(client: AsyncClient) -> None:
    response = await client.post(
        "/api/intents",
        json={
            "title": "impossible",
            "minutes_per_period": 240,
            "min_chunk_minutes": 240,
            "window_start_min": 600,
            "window_end_min": 630,
        },
    )
    assert response.status_code == 422
    assert "min_chunk" in response.json()["detail"]


@pytest.mark.asyncio
async def test_oversubscribed_calendar_reports_shortfall(client: AsyncClient) -> None:
    for i in range(40):
        await client.post(
            "/api/intents",
            json={"title": f"big {i}", "minutes_per_period": 480, "max_chunk_minutes": 480},
        )
    plan = (await client.get("/api/plan")).json()
    assert plan["complete"] is False
    assert plan["unmet"], "unplaceable demand must be reported, not dropped"
    assert plan["unmet"][0]["shortfall_minutes"] > 0


@pytest.mark.asyncio
async def test_accepts_floating_local_times(client: AsyncClient) -> None:
    """ICS files and browser `toISOString()` both hand over datetimes with no
    offset. Those must be read in the configured zone, not 500."""
    base = origin().replace(tzinfo=None)
    response = await client.put(
        "/api/busy",
        json=[
            {
                "label": "Naive meeting",
                "start": (base + timedelta(days=1, hours=10)).isoformat(),
                "end": (base + timedelta(days=1, hours=11)).isoformat(),
            }
        ],
    )
    assert response.status_code == 200
    plan = (await client.get("/api/plan")).json()
    assert plan["busy"][0]["label"] == "Naive meeting"
    # It comes back offset-aware, whatever the host's zone happens to be — the
    # point is that it was interpreted, not that it was interpreted as UTC.
    assert datetime.fromisoformat(plan["busy"][0]["start"]).tzinfo is not None


@pytest.mark.asyncio
async def test_smart_meeting_lands_in_shared_availability(client: AsyncClient) -> None:
    """A Smart Meeting must dodge the other attendees' calendars — while
    leaving those same hours free for the user's own solo work."""
    base = origin()
    busy_for_them = [
        {
            "start": (base + timedelta(days=d, hours=9)).isoformat(),
            "end": (base + timedelta(days=d, hours=15)).isoformat(),
            "attendee": "sam@example.com",
        }
        for d in range(7)
    ]
    await client.post(
        "/api/intents",
        json={
            "title": "Weekly sync",
            "kind": "meeting",
            "priority": 2,
            "minutes_per_period": 60,
            "min_chunk_minutes": 60,
            "max_chunk_minutes": 60,
            "attendee_busy": busy_for_them,
        },
    )
    await client.post(
        "/api/intents",
        json={
            "title": "Solo focus",
            "kind": "focus",
            "minutes_per_period": 120,
            "min_chunk_minutes": 120,
            "max_chunk_minutes": 120,
        },
    )
    plan = (await client.get("/api/plan")).json()

    meeting = next(b for b in plan["blocks"] if b["title"] == "Weekly sync")
    hour = datetime.fromisoformat(meeting["start"]).hour
    assert hour >= 15, "the meeting must avoid every attendee's blocked hours"

    focus = next(b for b in plan["blocks"] if b["title"] == "Solo focus")
    assert datetime.fromisoformat(focus["start"]).hour < 15, (
        "an attendee's calendar must not block the user's own solo work"
    )


def _mock_llm(
    monkeypatch: pytest.MonkeyPatch, handler: typing.Callable[[httpx.Request], httpx.Response]
) -> None:
    """Route the app's own outbound `httpx.AsyncClient` calls to `handler`,
    so a captured request never actually leaves the process."""
    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(**kw, transport=httpx.MockTransport(handler))
    )


@pytest.mark.asyncio
async def test_capture_when_the_model_is_unreachable_returns_503_not_500(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connection failure to the LLM backend (wrong HOROLOG_LLM_BASE_URL,
    the model server not running yet) is the operator's problem, not a crash —
    the endpoint must say so with a 503, never a bare 500."""

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _mock_llm(monkeypatch, handler)
    response = await client.post("/api/capture", json={"text": "gym 3x a week"})
    assert response.status_code == 503
    assert "could not reach" in response.json()["detail"]


@pytest.mark.asyncio
async def test_capture_when_the_model_returns_404_surfaces_the_body(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ollama's 404 body says 'model not found, try pulling it first' — that
    text is the one actionable thing here and must survive into the
    response, not get discarded by a generic status-code message."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="model 'qwen3:8b' not found, try pulling it first")

    _mock_llm(monkeypatch, handler)
    response = await client.post("/api/capture", json={"text": "gym 3x a week"})
    assert response.status_code == 503
    assert "not found, try pulling it first" in response.json()["detail"]


@pytest.mark.asyncio
async def test_capture_when_the_model_returns_an_unexpected_shape_returns_503_not_500(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 200 with no `choices` key — e.g. a reverse proxy's own error page,
    or a server that isn't actually OpenAI-chat-compatible — must not crash
    the endpoint with an unhandled KeyError."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "not the shape you expected"})

    _mock_llm(monkeypatch, handler)
    response = await client.post("/api/capture", json={"text": "gym 3x a week"})
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_capture_with_anthropic_selected_but_sdk_missing_returns_503_not_500(
    client: AsyncClient,
) -> None:
    """`anthropic` is an optional extra, not in the documented dev install
    (`.[dev]`) — only the Docker image has it. Picking "Anthropic" in the UI
    without it installed must surface the friendly install message, not a
    bare 500. No mocking needed: the package genuinely isn't in this venv."""
    response = await client.post(
        "/api/capture",
        json={"text": "gym 3x a week", "provider": "anthropic", "model": "claude-opus-5"},
    )
    assert response.status_code == 503
    assert "horolog[anthropic]" in response.json()["detail"]


@pytest.mark.asyncio
async def test_sync_ics_when_the_feed_is_unreachable_returns_502_not_500(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead or mistyped .ics URL is the user's mistake to fix, not a server
    crash — must map to 502 with the reason included."""

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("name or service not known")

    _mock_llm(monkeypatch, handler)
    response = await client.post(
        "/api/sync/ics", json={"url": "https://example.invalid/calendar.ics"}
    )
    assert response.status_code == 502


# --------------------------------------------------------------- task lifecycle


@pytest.mark.asyncio
async def test_completing_a_task_frees_its_capacity_on_the_next_solve(
    client: AsyncClient,
) -> None:
    created = (
        await client.post(
            "/api/intents", json={"title": "Ship the thing", "minutes_per_period": 60}
        )
    ).json()

    plan = (await client.get("/api/plan")).json()
    assert any(b["intent_id"] == created["id"] for b in plan["blocks"])

    done = await client.post(f"/api/intents/{created['id']}/complete")
    assert done.status_code == 200
    assert done.json()["completed_at"] is not None

    plan = (await client.get("/api/plan")).json()
    assert not any(b["intent_id"] == created["id"] for b in plan["blocks"]), (
        "a completed task must not keep occupying a slot"
    )

    intents = (await client.get("/api/intents")).json()
    mine = next(i for i in intents if i["id"] == created["id"])
    assert mine["completed_at"] is not None, "the row is kept, not deleted"


@pytest.mark.asyncio
async def test_uncompleting_a_task_restores_it_to_the_plan(client: AsyncClient) -> None:
    created = (
        await client.post("/api/intents", json={"title": "Redo it", "minutes_per_period": 60})
    ).json()
    await client.post(f"/api/intents/{created['id']}/complete")

    undone = await client.delete(f"/api/intents/{created['id']}/complete")
    assert undone.status_code == 200
    assert undone.json()["completed_at"] is None

    plan = (await client.get("/api/plan")).json()
    assert any(b["intent_id"] == created["id"] for b in plan["blocks"])


@pytest.mark.asyncio
async def test_completing_a_recurring_habit_is_rejected(client: AsyncClient) -> None:
    """The whole recurring series cannot be completed by the one-shot route."""
    created = (
        await client.post(
            "/api/intents",
            json={
                "title": "Gym",
                "kind": "habit",
                "minutes_per_period": 180,
                "period_days": 7,
                "min_chunk_minutes": 60,
                "max_chunk_minutes": 60,
            },
        )
    ).json()
    response = await client.post(f"/api/intents/{created['id']}/complete")
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_recurring_session_completion_survives_replanning_and_can_be_undone(
    client: AsyncClient,
) -> None:
    created = (
        await client.post(
            "/api/intents",
            json={
                "title": "Read",
                "kind": "habit",
                "minutes_per_period": 60,
                "period_days": 7,
                "min_chunk_minutes": 60,
                "max_chunk_minutes": 60,
            },
        )
    ).json()
    path = f"/api/intents/{created['id']}"
    initial = (await client.get("/api/plan")).json()["blocks"]
    mine = [block for block in initial if block["intent_id"] == created["id"]]
    assert len(mine) >= 2
    day = mine[0]["start"][:10]

    marked = await client.put(f"{path}/completed-dates/{day}")
    assert marked.status_code == 200
    assert marked.json()["completed_dates"] == [day]
    await client.post("/api/plan/solve")
    after = (await client.get("/api/plan")).json()["blocks"]
    assert all(block["completed"] == (block["start"][:10] == day)
               for block in after if block["intent_id"] == created["id"])
    assert (await client.get(path)).json()["completed_dates"] == [day]

    undone = await client.delete(f"{path}/completed-dates/{day}")
    assert undone.status_code == 200
    assert undone.json()["completed_dates"] == []
    assert not any(block["completed"] for block in (await client.get("/api/plan")).json()["blocks"]
                   if block["intent_id"] == created["id"])


@pytest.mark.asyncio
async def test_completing_an_unknown_intent_is_404(client: AsyncClient) -> None:
    response = await client.post("/api/intents/does-not-exist/complete")
    assert response.status_code == 404


def _no_overlaps(blocks: list[dict[str, typing.Any]], intent_id: str) -> bool:
    mine = sorted(
        (datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"]))
        for b in blocks
        if b["intent_id"] == intent_id
    )
    return all(mine[i][1] <= mine[i + 1][0] for i in range(len(mine) - 1))


@pytest.mark.asyncio
async def test_editing_an_intent_keeps_its_id_and_stays_stable_when_unchanged(
    client: AsyncClient,
) -> None:
    created = (
        await client.post(
            "/api/intents", json={"title": "Original", "minutes_per_period": 60, "priority": 3}
        )
    ).json()
    before = (await client.get("/api/plan")).json()
    was = next(b for b in before["blocks"] if b["intent_id"] == created["id"])

    edited = await client.put(
        f"/api/intents/{created['id']}",
        json={"title": "Original", "minutes_per_period": 60, "priority": 1},
    )
    assert edited.status_code == 200
    assert edited.json()["id"] == created["id"]

    after = (await client.get("/api/plan")).json()
    now = next(b for b in after["blocks"] if b["intent_id"] == created["id"])
    assert now["start"] == was["start"], "an edit that doesn't touch duration must not move it"
    assert now["priority"] == 1


@pytest.mark.asyncio
async def test_editing_an_intent_larger_does_not_overlap_or_orphan(client: AsyncClient) -> None:
    created = (
        await client.post(
            "/api/intents",
            json={
                "title": "Grows",
                "minutes_per_period": 120,
                "min_chunk_minutes": 30,
                "max_chunk_minutes": 120,
            },
        )
    ).json()

    edited = await client.put(
        f"/api/intents/{created['id']}",
        json={
            "title": "Grows",
            "minutes_per_period": 180,
            "min_chunk_minutes": 30,
            "max_chunk_minutes": 120,
        },
    )
    assert edited.status_code == 200

    plan = (await client.get("/api/plan")).json()
    assert _no_overlaps(plan["blocks"], created["id"])
    placed = sum(
        (datetime.fromisoformat(b["end"]) - datetime.fromisoformat(b["start"])).total_seconds()
        for b in plan["blocks"]
        if b["intent_id"] == created["id"]
    )
    assert placed == 180 * 60


@pytest.mark.asyncio
async def test_editing_an_intent_smaller_does_not_overlap_or_orphan(client: AsyncClient) -> None:
    created = (
        await client.post(
            "/api/intents",
            json={
                "title": "Shrinks",
                "minutes_per_period": 180,
                "min_chunk_minutes": 30,
                "max_chunk_minutes": 120,
            },
        )
    ).json()

    edited = await client.put(
        f"/api/intents/{created['id']}",
        json={
            "title": "Shrinks",
            "minutes_per_period": 60,
            "min_chunk_minutes": 30,
            "max_chunk_minutes": 120,
        },
    )
    assert edited.status_code == 200

    plan = (await client.get("/api/plan")).json()
    assert _no_overlaps(plan["blocks"], created["id"])
    placed = sum(
        (datetime.fromisoformat(b["end"]) - datetime.fromisoformat(b["start"])).total_seconds()
        for b in plan["blocks"]
        if b["intent_id"] == created["id"]
    )
    assert placed == 60 * 60


@pytest.mark.asyncio
async def test_editing_an_unknown_intent_is_404(client: AsyncClient) -> None:
    response = await client.put(
        "/api/intents/does-not-exist", json={"title": "x", "minutes_per_period": 30}
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_editing_an_intent_rejects_the_same_invalid_shapes_as_creating_one(
    client: AsyncClient,
) -> None:
    created = (
        await client.post("/api/intents", json={"title": "Fine", "minutes_per_period": 60})
    ).json()
    response = await client.put(
        f"/api/intents/{created['id']}",
        json={
            "title": "Fine",
            "minutes_per_period": 60,
            "min_chunk_minutes": 90,
            "max_chunk_minutes": 30,
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_patch_preserves_constraints_and_appearance_does_not_replan(client):
    from unittest.mock import AsyncMock, patch

    created = (
        await client.post(
            "/api/intents",
            json={
                "title": "Keep constraints",
                "minutes_per_period": 60,
                "due": (origin() + timedelta(days=4)).isoformat(),
                "earliest": (origin() + timedelta(days=1)).isoformat(),
                "allowed_weekdays": [0, 2, 4],
                "tags": ["work"],
            },
        )
    ).json()
    path = f"/api/intents/{created['id']}"
    before = (await client.get("/api/plan")).json()["blocks"]
    with patch("horolog.api._replan", new_callable=AsyncMock) as replan:
        result = await client.patch(path, json={"color": "#12aBcD", "add_tags": ["home"]})
        replan.assert_not_awaited()
    assert result.status_code == 200
    saved = (await client.get(path)).json()
    assert saved == created | {"color": "#12aBcD", "tags": ["work", "home"]}
    plan = (await client.get("/api/plan")).json()
    assert [(b["start"], b["end"]) for b in plan["blocks"]] == [
        (b["start"], b["end"]) for b in before
    ]
    assert all(b["color"] == "#12aBcD" and b["tags"] == ["work", "home"] for b in plan["blocks"])
    assert (await client.patch(path, json={"remove_tags": ["work"]})).json()["tags"] == ["home"]
    assert (await client.patch(path, json={"tags": [], "color": None})).json()["color"] is None
    assert (await client.get(path)).json()["tags"] == []


@pytest.mark.asyncio
async def test_patch_validates_complete_model_and_explicit_null(client):
    created = (
        await client.post(
            "/api/intents",
            json={
                "title": "Validation",
                "minutes_per_period": 60,
            },
        )
    ).json()
    path = f"/api/intents/{created['id']}"
    for changes in [
        {"max_chunk_minutes": 15},
        {"minutes_per_period": 10},
        {"allowed_weekdays": [7]},
        {"allowed_weekdays": [1, 1]},
        {"title": None},
        {"tags": None},
        {"color": "invalid"},
        {"completed_at": "2026-01-01"},
        {"tags": [" "]},
        {"daily_windows": [{"start_min": 600, "end_min": 610}]},
    ]:
        assert (await client.patch(path, json=changes)).status_code == 422
        assert (await client.get(path)).json() == created
    assert (await client.patch("/api/intents/missing", json={})).status_code == 404
    recurring = await client.patch(
        path,
        json={
            "period_days": 7,
            "kind": "habit",
            "allowed_weekdays": [0, 6],
            "minutes_per_period": 120,
            "min_chunk_minutes": 60,
            "max_chunk_minutes": 60,
            "max_per_day": 1,
        },
    )
    assert recurring.status_code == 200
    assert (await client.post(path + "/complete")).status_code == 422
    assert (
        await client.patch(path, json={"period_days": None, "allowed_weekdays": None})
    ).status_code == 200
    completed = (await client.post(path + "/complete")).json()
    assert (await client.patch(path, json={"title": "Done"})).json()["completed_at"] == completed[
        "completed_at"
    ]
    assert (await client.patch(path, json={"period_days": 7})).status_code == 422
    assert (await client.delete(path + "/complete")).json()["completed_at"] is None


@pytest.mark.asyncio
async def test_patch_preserves_server_fields_and_multiple_windows(client):
    from horolog.db import IntentRow

    created = (
        await client.post(
            "/api/intents",
            json={
                "title": "Meeting",
                "minutes_per_period": 60,
            },
        )
    ).json()
    path = f"/api/intents/{created['id']}"
    gen = session()
    db = await anext(gen)
    row = await db.get(IntentRow, created["id"])
    row.payload = row.payload | {
        "zoom_meeting_id": "test-only",
        "zoom_join_url": "https://example.test/meeting",
        "daily_windows": [{"start_min": 540, "end_min": 660}, {"start_min": 840, "end_min": 960}],
        "blocked_slots": [[36, 40]],
    }
    await db.commit()
    await gen.aclose()
    before = (await client.get(path)).json()
    after = (await client.patch(path, json={"tags": ["meeting"]})).json()
    assert after == before | {"tags": ["meeting"]}
    after = (await client.patch(path, json={"window_start_min": 570})).json()
    assert after["daily_windows"] == [
        {"start_min": 570, "end_min": 660},
        {"start_min": 840, "end_min": 960},
    ]


@pytest.mark.asyncio
async def test_patch_datetime_localisation_and_clearing_constraints(client):
    from horolog.domain.time import to_slot
    from horolog.settings import settings

    created = (
        await client.post(
            "/api/intents",
            json={
                "title": "Dates",
                "minutes_per_period": 60,
            },
        )
    ).json()
    path = f"/api/intents/{created['id']}"
    local_due = (origin() + timedelta(days=3, hours=12)).replace(tzinfo=None)
    result = await client.patch(path, json={"due": local_due.isoformat()})
    assert result.status_code == 200
    assert result.json()["due_slot"] == to_slot(local_due.replace(tzinfo=settings().zone), origin())
    aware = (origin() + timedelta(days=1)).astimezone(UTC)
    result = await client.patch(path, json={"earliest": aware.isoformat()})
    assert result.json()["earliest_slot"] == to_slot(aware, origin())
    assert (await client.patch(path, json={"due": aware.isoformat()})).status_code == 422
    cleared = (
        await client.patch(
            path,
            json={
                "due": None,
                "earliest": None,
                "daily_windows": [],
                "attendee_busy": [],
            },
        )
    ).json()
    assert cleared["due_slot"] is None and cleared["earliest_slot"] is None
    assert cleared["daily_windows"] == [] and cleared["blocked_slots"] == []


@pytest.mark.asyncio
async def test_all_day_calendar_block_preserves_other_events_and_tasks(client):
    from unittest.mock import patch

    from horolog.settings import settings

    base = datetime(2026, 10, 20, tzinfo=settings().zone)
    with patch("horolog.api.origin", return_value=base):
        await client.put(
            "/api/busy",
            json=[
                {
                    "label": "Existing commitment",
                    "start": (base + timedelta(hours=10)).isoformat(),
                    "end": (base + timedelta(hours=11)).isoformat(),
                }
            ],
        )
        task = (
            await client.post(
                "/api/intents",
                json={
                    "title": "College work",
                    "minutes_per_period": 60,
                    "period_days": 7,
                    "allowed_weekdays": [1],
                    "min_chunk_minutes": 60,
                    "max_chunk_minutes": 60,
                    "max_per_day": 1,
                },
            )
        ).json()
        baseline_unmet = (await client.get("/api/plan")).json()["unmet"]
        result = await client.post(
            "/api/calendar-blocks",
            json={
                "title": "College final — San Diego",
                "all_day_date": "2026-10-27",
            },
        )
        assert result.status_code == 201
        block = result.json()
        assert block["all_day"] is True
        assert block["start"].startswith("2026-10-27T00:00:00")
        assert block["end"].startswith("2026-10-28T00:00:00")
        plan = block["plan"]
        assert any(b["label"] == "Existing commitment" for b in plan["busy"])
        fixed = [b for b in plan["busy"] if b["source"] == "calendar-block"]
        assert len(fixed) == 1
        assert datetime.fromisoformat(fixed[0]["start"]) == datetime.fromisoformat(block["start"])
        assert all(not b["start"].startswith("2026-10-27") for b in plan["blocks"])
        openings = (await client.get("/api/availability?days=14&minutes=60")).json()
        assert all(not slot["start"].startswith("2026-10-27") for slot in openings)
        assert any(
            u["intent_id"] == task["id"] and u["shortfall_minutes"] == 60 for u in plan["unmet"]
        )
        assert (await client.get("/api/intents/" + task["id"])).json() == task
        listed = (await client.get("/api/calendar-blocks")).json()
        assert listed == [{k: v for k, v in block.items() if k != "plan"}]
        # Batch replacement of manual busy events must not erase fixed date blocks.
        await client.put("/api/busy", json=[])
        assert len((await client.get("/api/calendar-blocks")).json()) == 1
        assert (await client.delete("/api/calendar-blocks/" + block["id"])).status_code == 204
        plan = (await client.get("/api/plan")).json()
        assert plan["unmet"] == baseline_unmet
        assert any(b["start"].startswith("2026-10-27") for b in plan["blocks"])


@pytest.mark.asyncio
async def test_calendar_blocks_keep_dates_across_midnight_and_dst(client):
    from unittest.mock import patch

    from horolog.settings import settings

    base = datetime(2026, 10, 20, tzinfo=settings().zone)
    with patch("horolog.api.origin", return_value=base):
        block = (
            await client.post(
                "/api/calendar-blocks",
                json={
                    "title": "Away",
                    "all_day_date": "2026-10-27",
                    "end_date": "2026-10-29",
                },
            )
        ).json()
    with patch("horolog.api.origin", return_value=base + timedelta(days=1)):
        plan = (await client.post("/api/plan/solve")).json()
        fixed = [b for b in plan["busy"] if b["source"] == "calendar-block"]
        assert fixed[0]["start"].startswith("2026-10-27T00:00:00")
        assert fixed[0]["end"].startswith("2026-10-29T00:00:00")
    await client.delete("/api/calendar-blocks/" + block["id"])
    from zoneinfo import ZoneInfo

    with (
        patch(
            "horolog.api.settings",
            return_value=settings().model_copy(update={"timezone": "America/Los_Angeles"}),
        ),
        patch(
            "horolog.api.origin",
            return_value=datetime(2026, 10, 20, tzinfo=ZoneInfo("America/Los_Angeles")),
        ),
    ):
        dst = (
            await client.post(
                "/api/calendar-blocks",
                json={
                    "title": "DST absence",
                    "all_day_date": "2026-11-01",
                },
            )
        ).json()
        assert dst["start"] == "2026-11-01T00:00:00-07:00"
        assert dst["end"] == "2026-11-02T00:00:00-08:00"
        await client.delete("/api/calendar-blocks/" + dst["id"])


@pytest.mark.asyncio
async def test_timed_calendar_blocks_round_outwards_and_validate(client):
    base = origin()
    result = await client.post(
        "/api/calendar-blocks",
        json={
            "title": "Appointment",
            "start": (base + timedelta(hours=9, minutes=7)).astimezone(UTC).isoformat(),
            "end": (base + timedelta(hours=9, minutes=22)).astimezone(UTC).isoformat(),
        },
    )
    assert result.status_code == 201
    block = result.json()
    fixed = [b for b in block["plan"]["busy"] if b["source"] == "calendar-block"]
    assert datetime.fromisoformat(fixed[0]["start"]) == base + timedelta(hours=9)
    assert datetime.fromisoformat(fixed[0]["end"]) == base + timedelta(hours=9, minutes=30)
    for body in [
        {"title": "Missing dates"},
        {"title": " ", "all_day_date": "2026-10-27"},
        {"title": "Wrong order", "all_day_date": "2026-10-27", "end_date": "2026-10-26"},
        {"title": "Mixed", "all_day_date": "2026-10-27", "start": base.isoformat()},
        {"title": "Wrong date", "all_day_date": "2026-02-30"},
        {"title": "Wrong order", "start": base.isoformat(), "end": base.isoformat()},
    ]:
        assert (await client.post("/api/calendar-blocks", json=body)).status_code == 422
    assert len((await client.get("/api/calendar-blocks")).json()) == 1
    assert (await client.delete("/api/calendar-blocks/missing")).status_code == 404
    await client.delete("/api/calendar-blocks/" + block["id"])


@pytest.mark.asyncio
async def test_cached_plan_expires_after_local_midnight(client):
    from horolog.db import PlanRow, load_previous_plan

    gen = session()
    db = await anext(gen)
    row = await db.get(PlanRow, 1)
    row.saved_at = datetime.now(UTC) - timedelta(days=1)
    await db.commit()
    assert await load_previous_plan(db) is None
    await gen.aclose()
    assert (await client.get("/api/plan")).status_code == 200


@pytest.mark.asyncio
async def test_creation_retry_keys_and_conflicting_input(client):
    import asyncio
    import uuid

    for endpoint, body in [
        ("/api/intents", {"title": "Retry task", "minutes_per_period": 60}),
        ("/api/calendar-blocks", {"title": "Retry trip", "all_day_date": "2026-10-27"}),
    ]:
        headers = {"Idempotency-Key": uuid.uuid4().hex}
        # Concurrent identical requests must serialize on the DB uniqueness constraint.
        first, second = await asyncio.gather(
            *[client.post(endpoint, json=body, headers=headers) for _ in range(2)]
        )
        assert first.status_code == second.status_code == 201
        assert first.json()["id"] == second.json()["id"]
        different = await client.post(endpoint, json=body | {"title": "Different"}, headers=headers)
        assert different.status_code == 409
        another = await client.post(endpoint, json=body)
        assert another.status_code == 201 and another.json()["id"] != first.json()["id"]
        await client.delete(endpoint + "/" + first.json()["id"])
        assert (await client.post(endpoint, json=body, headers=headers)).status_code == 409
        await client.delete(endpoint + "/" + another.json()["id"])


@pytest.mark.asyncio
async def test_weekly_edits_preserve_session_length_and_support_skip(client):
    from unittest.mock import patch

    base = origin()
    weekdays = [base.weekday(), (base.weekday() + 2) % 7, (base.weekday() + 4) % 7]
    task = (
        await client.post(
            "/api/intents",
            json={
                "title": "Study",
                "kind": "habit",
                "period_days": 7,
                "minutes_per_period": 135,
                "allowed_weekdays": weekdays,
                "min_chunk_minutes": 45,
                "max_chunk_minutes": 45,
                "max_per_day": 1,
            },
        )
    ).json()
    path = "/api/intents/" + task["id"]
    updated = (await client.patch(path, json={"allowed_weekdays": weekdays[1:]})).json()
    assert updated["minutes_per_period"] == 90 and updated["min_chunk_minutes"] == 45
    updated = (await client.patch(path, json={"session_minutes": 60})).json()
    assert updated["minutes_per_period"] == 120 and updated["max_chunk_minutes"] == 60
    assert (
        await client.patch(path, json={"session_minutes": 60, "minutes_per_period": 180})
    ).status_code == 422
    skip_day = (base + timedelta(days=2)).date().isoformat()
    result = await client.post(path + "/skips/" + skip_day)
    assert result.status_code == 200
    assert result.json()["task"]["skipped_dates"] == [skip_day]
    assert (await client.post(path + "/skips/" + skip_day)).json()["task"]["skipped_dates"] == [
        skip_day
    ]
    assert all(not b["start"].startswith(skip_day) for b in result.json()["plan"]["blocks"])
    assert (await client.post(path + "/complete")).status_code == 422
    assert (await client.patch(path, json={"period_days": None})).status_code == 422
    assert (await client.delete(path + "/skips/" + skip_day)).json()["task"]["skipped_dates"] == []
    assert (
        await client.post(path + "/skips/" + (base + timedelta(days=1)).date().isoformat())
    ).status_code == 422
    budget = (
        await client.post(
            "/api/intents",
            json={
                "title": "Weekly goal",
                "minutes_per_period": 120,
                "period_days": 7,
            },
        )
    ).json()
    assert (
        await client.post("/api/intents/" + budget["id"] + "/skips/" + skip_day)
    ).status_code == 422
    with patch("horolog.api._replan") as replan:
        # Explicit total demand still wins when no session shorthand is used.
        replan.return_value = None
        result = await client.patch(
            path, json={"allowed_weekdays": weekdays, "minutes_per_period": 180}
        )
        assert result.status_code == 200


@pytest.mark.asyncio
async def test_edit_fixed_appointments_preserves_ids_and_validates(client):
    block = (
        await client.post(
            "/api/calendar-blocks",
            json={
                "title": "Trip",
                "all_day_date": "2026-10-27",
                "end_date": "2026-10-29",
            },
        )
    ).json()
    path = "/api/calendar-blocks/" + block["id"]
    moved = (await client.patch(path, json={"all_day_date": "2026-10-28"})).json()
    assert moved["id"] == block["id"] and moved["end"].startswith("2026-10-30")
    renamed = (await client.patch(path, json={"title": "College trip"})).json()
    assert renamed["start"] == moved["start"] and renamed["end"] == moved["end"]
    assert (await client.patch(path, json={"end_date": "2026-10-27"})).status_code == 422
    assert (await client.patch(path, json={"title": None})).status_code == 422
    converted = await client.patch(
        path,
        json={
            "start": "2026-10-28T10:00:00-07:00",
            "end": "2026-10-28T11:00:00-07:00",
        },
    )
    assert converted.status_code == 200 and not converted.json()["all_day"]
    assert (await client.patch(path, json={"end": "2026-10-28T09:00:00-07:00"})).status_code == 422
    back = (await client.patch(path, json={"all_day_date": "2026-10-29"})).json()
    assert back["all_day"] and back["end"].startswith("2026-10-30")
    await client.delete(path)


@pytest.mark.asyncio
async def test_dates_remain_fixed_after_rollover_and_appearance_edit(client):
    from unittest.mock import patch

    from horolog.domain.time import from_slot

    base = origin()
    due = base + timedelta(days=4, hours=12)
    earliest = base + timedelta(days=1, hours=9)
    task = (
        await client.post(
            "/api/intents",
            json={
                "title": "Stable dates",
                "minutes_per_period": 60,
                "due": due.isoformat(),
                "earliest": earliest.isoformat(),
                "attendee_busy": [
                    {
                        "start": (base + timedelta(days=2, hours=10)).isoformat(),
                        "end": (base + timedelta(days=2, hours=11)).isoformat(),
                    }
                ],
            },
        )
    ).json()
    path = "/api/intents/" + task["id"]
    tomorrow = base + timedelta(days=1)
    with patch("horolog.api.origin", return_value=tomorrow):
        updated = (await client.patch(path, json={"color": "#123456"})).json()
        assert from_slot(updated["due_slot"], tomorrow) == due
        assert from_slot(updated["earliest_slot"], tomorrow) == earliest
        assert from_slot(updated["blocked_slots"][0][0], tomorrow) == base + timedelta(
            days=2, hours=10
        )
    with patch("horolog.api.origin", return_value=tomorrow + timedelta(days=1)):
        saved = (await client.get(path)).json()
        assert from_slot(saved["due_slot"], tomorrow + timedelta(days=1)) == due
    cleared = (await client.patch(path, json={"due": None, "earliest": None})).json()
    assert cleared["due_slot"] is None and cleared["earliest_slot"] is None


@pytest.mark.asyncio
async def test_new_placements_are_in_future_and_idle_differs_from_bookable(client):
    from unittest.mock import patch

    now = origin() + timedelta(hours=10, minutes=7)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    with patch("horolog.api.datetime", Clock):
        task = await client.post(
            "/api/intents",
            json={
                "title": "Created at 10:07",
                "minutes_per_period": 60,
            },
        )
        assert task.status_code == 201
        plan = (await client.get("/api/plan")).json()
        assert plan["blocks"]
        assert all(datetime.fromisoformat(b["start"]) >= now for b in plan["blocks"])
        bookable = (await client.get("/api/availability?minutes=60&days=7")).json()
        idle = (await client.get("/api/availability?minutes=60&days=7&mode=idle")).json()
        assert len(bookable) > len(idle)
        for slot in idle:
            start, end = datetime.fromisoformat(slot["start"]), datetime.fromisoformat(slot["end"])
            assert not any(
                start < datetime.fromisoformat(b["end"])
                and datetime.fromisoformat(b["start"]) < end
                for b in plan["blocks"]
            )
        assert (await client.get("/api/availability?mode=unknown")).status_code == 422


@pytest.mark.asyncio
async def test_late_placements_are_reported(client):
    due = origin() - timedelta(days=1)
    task = (
        await client.post(
            "/api/intents",
            json={
                "title": "Overdue work",
                "minutes_per_period": 60,
                "due": due.isoformat(),
            },
        )
    ).json()
    blocks = [
        b for b in (await client.get("/api/plan")).json()["blocks"] if b["intent_id"] == task["id"]
    ]
    assert blocks and all(b["late_by_minutes"] > 0 for b in blocks)
    assert all(datetime.fromisoformat(b["deadline"]) == due for b in blocks)


@pytest.mark.asyncio
async def test_legacy_slot_dates_are_anchored_once_without_reset(client):
    from horolog.db import IntentRow

    task = (
        await client.post(
            "/api/intents",
            json={
                "title": "Legacy date",
                "minutes_per_period": 60,
                "due": (origin() + timedelta(days=3)).isoformat(),
            },
        )
    ).json()
    gen = session()
    db = await anext(gen)
    row = await db.get(IntentRow, task["id"])
    legacy = dict(row.payload)
    legacy.pop("slot_origin")
    row.payload = legacy
    await db.commit()
    await gen.aclose()
    await init_db()
    saved = (await client.get("/api/intents/" + task["id"])).json()
    assert saved["slot_origin"] is not None
    assert saved["due_slot"] == task["due_slot"]
    await init_db()
    assert (await client.get("/api/intents/" + task["id"])).json() == saved


@pytest.mark.asyncio
async def test_partial_slot_bounds_never_schedule_before_earliest_or_inside_attendee_busy(client):
    from horolog.domain.time import to_slot

    base = origin()
    earliest = base + timedelta(days=1, hours=9, minutes=7)
    body = {
        "title": "Precise bounds",
        "minutes_per_period": 30,
        "earliest": earliest.isoformat(),
        "attendee_busy": [
            {
                "start": (base + timedelta(days=1, hours=10, minutes=7)).isoformat(),
                "end": (base + timedelta(days=1, hours=10, minutes=22)).isoformat(),
            }
        ],
    }
    task = (await client.post("/api/intents", json=body)).json()
    assert task["earliest_slot"] == to_slot(earliest, base) + 1
    assert task["blocked_slots"][0][1] == 96 + 42
    bad = body | {"attendee_busy": [{"start": earliest.isoformat(), "end": earliest.isoformat()}]}
    assert (await client.post("/api/intents", json=bad)).status_code == 422
    assert (
        await client.patch(
            "/api/intents/" + task["id"], json={"attendee_busy": bad["attendee_busy"]}
        )
    ).status_code == 422
