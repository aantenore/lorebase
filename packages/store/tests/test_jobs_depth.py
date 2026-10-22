"""The queue depth tells claimable jobs from jobs waiting for a retry delay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from lorebase_store.jobs import claim_jobs, enqueue_job, finish_job, queue_depth
from lorebase_store.settings import JobSettings

pytestmark = pytest.mark.integration

JOB = "webhook_delivery"


async def test_queue_depth_separates_queued_retrying_and_running(engine: AsyncEngine) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC) + timedelta(seconds=1)
    settings = JobSettings(max_attempts=3, retry_delay_seconds=60)
    async with sessions() as session, session.begin():
        for index in range(3):
            await enqueue_job(session, JOB, payload={"n": index})
    async with sessions() as session, session.begin():
        (failed,) = await claim_jobs(session, "w1", now=now)
        assert await finish_job(session, failed, error="boom", settings=settings, now=now) == (
            "queued"
        )
        await claim_jobs(session, "w1", now=now)
        depth = await queue_depth(session, now=now)
    assert depth == {(JOB, "queued"): 1, (JOB, "retrying"): 1, (JOB, "running"): 1}

    async with sessions() as session, session.begin():
        later = await queue_depth(session, now=now + timedelta(seconds=61))
    assert later == {(JOB, "queued"): 2, (JOB, "running"): 1}
