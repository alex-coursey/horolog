"""Opt-in check against a disposable PostgreSQL database, never production.

Run with HOROLOG_TEST_POSTGRES_URL pointing at a newly created throwaway DB.
The test creates tables and writes only a unique test intent; it never drops tables.
"""

import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from horolog.db import Base, CalendarBlockRow, IntentRow
from horolog.domain.intent import Intent


@pytest.mark.asyncio
async def test_postgres_color_tags_round_trip():
    url = os.environ.get("HOROLOG_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("requires an explicitly configured disposable PostgreSQL database")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ident = "integration-" + uuid.uuid4().hex
    intent = Intent(
        id=ident,
        kind="habit",
        title="Persistence verification",
        color="#12ABef",
        tags=["work", "test"],
        minutes_per_period=120,
        period_days=7,
        min_chunk_minutes=60,
        max_chunk_minutes=60,
        allowed_weekdays=[0, 6],
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as db:
            db.add(IntentRow(id=ident, payload=intent.model_dump(mode="json")))
            db.add(
                CalendarBlockRow(
                    id=ident,
                    title="College final — San Diego",
                    all_day=True,
                    start_at=datetime(2026, 10, 27, 7, tzinfo=UTC),
                    end_at=datetime(2026, 10, 28, 7, tzinfo=UTC),
                )
            )
            await db.commit()
        async with factory() as db:
            row = await db.get(IntentRow, ident)
            assert row.to_domain() == intent
            block = await db.get(CalendarBlockRow, ident)
            assert block.all_day and block.start_at == datetime(2026, 10, 27, 7, tzinfo=UTC)
            assert block.end_at == datetime(2026, 10, 28, 7, tzinfo=UTC)
            changed = Intent.model_validate(row.payload | {"color": None, "tags": ["updated"]})
            row.payload = changed.model_dump(mode="json")
            await db.commit()
        async with factory() as db:
            row = await db.get(IntentRow, ident)
            assert row.to_domain() == changed
            await db.execute(delete(IntentRow).where(IntentRow.id == ident))
            await db.execute(delete(CalendarBlockRow).where(CalendarBlockRow.id == ident))
            await db.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_concurrent_creation_retries():
    import asyncio
    import hashlib

    from httpx import ASGITransport, AsyncClient

    from horolog.api import app
    from horolog.db import CreationRequestRow, session

    url = os.environ.get("HOROLOG_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("requires an explicitly configured disposable PostgreSQL database")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def isolated_session():
        async with factory() as db:
            yield db

    old = app.dependency_overrides.get(session)
    app.dependency_overrides[session] = isolated_session
    keys = []
    resources = []
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for endpoint, scope, body in [
                ("/api/intents", "intent", {"title": "Concurrent task", "minutes_per_period": 60}),
                (
                    "/api/calendar-blocks",
                    "calendar-block",
                    {"title": "Concurrent trip", "all_day_date": "2026-10-27"},
                ),
            ]:
                key = uuid.uuid4().hex
                keys.append(hashlib.sha256(f"{scope}:{key}".encode()).hexdigest())
                responses = await asyncio.gather(
                    *[
                        client.post(endpoint, json=body, headers={"Idempotency-Key": key})
                        for _ in range(3)
                    ]
                )
                assert all(response.status_code == 201 for response in responses)
                ids = {response.json()["id"] for response in responses}
                assert len(ids) == 1
                resources.append((endpoint, ids.pop()))
            for endpoint, ident in resources:
                assert (await client.delete(endpoint + "/" + ident)).status_code == 204
            resources.clear()
    finally:
        async with factory() as db:
            for endpoint, ident in resources:
                model = IntentRow if endpoint == "/api/intents" else CalendarBlockRow
                await db.execute(delete(model).where(model.id == ident))
            await db.execute(delete(CreationRequestRow).where(CreationRequestRow.id.in_(keys)))
            await db.commit()
        if old is None:
            app.dependency_overrides.pop(session, None)
        else:
            app.dependency_overrides[session] = old
        await engine.dispose()
