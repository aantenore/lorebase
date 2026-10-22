"""A starting Scribe that runs lineage backfills queues the sweep once, however often it starts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_scribe.service import Scribe
from lorebase_store.jobs import LINEAGE_BACKFILL_JOB

pytestmark = pytest.mark.integration

SWEEPS = text("SELECT status::text FROM jobs WHERE type = :type AND domain_id IS NULL")


async def serve_once(sessions: async_sessionmaker[AsyncSession]) -> None:
    """Start a Scribe whose job loop stops at once."""

    async def work(stop: asyncio.Event) -> None:
        await stop.wait()

    scribe = Scribe(
        engine=None,  # type: ignore[arg-type]
        context=SimpleNamespace(sessions=sessions, runtime=None),  # type: ignore[arg-type]
        worker=SimpleNamespace(types=[LINEAGE_BACKFILL_JOB], run=work),  # type: ignore[arg-type]
        scheduler=None,  # type: ignore[arg-type]
        settings=SimpleNamespace(scheduler=SimpleNamespace(enabled=False)),  # type: ignore[arg-type]
    )
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(scribe.serve(stop), timeout=10)


async def sweeps(sessions: async_sessionmaker[AsyncSession]) -> list[str]:
    async with sessions() as session:
        result = await session.execute(SWEEPS, {"type": LINEAGE_BACKFILL_JOB})
        return [status for (status,) in result]


async def test_startup_queues_the_lineage_sweep_once(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await serve_once(sessions)
    await serve_once(sessions)
    assert await sweeps(sessions) == ["queued"]
    # Two workers starting together still queue one.
    await asyncio.gather(serve_once(sessions), serve_once(sessions))
    assert await sweeps(sessions) == ["queued"]
    # Once it ran, a restart queues nothing.
    async with sessions() as session, session.begin():
        await session.execute(
            text("UPDATE jobs SET status = 'succeeded' WHERE type = :type"),
            {"type": LINEAGE_BACKFILL_JOB},
        )
    await serve_once(sessions)
    assert await sweeps(sessions) == ["succeeded"]
