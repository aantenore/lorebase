"""A webhook delivery pass that outlasts the lease of its deliveries is abandoned."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from lorebase_scribe import jobs
from lorebase_scribe.jobs import WEBHOOK_DELIVERY_JOB, JobContext, build_handlers
from lorebase_store.jobs import JobRow
from lorebase_store.webhooks import WebhookSettings


async def test_a_pass_outlasting_the_lease_fails_the_job_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def stalled(*args: Any, **kwargs: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(jobs, "deliver_pending", stalled)
    settings = WebhookSettings(
        timeout_s=0.1, connect_timeout_s=0.1, pass_timeout_s=0.1, lease_s=0.3
    )
    async with httpx.AsyncClient() as http:
        context = cast(
            JobContext,
            SimpleNamespace(
                sessions=None,
                webhooks=settings,
                http=http,
                runtime=SimpleNamespace(
                    settings=SimpleNamespace(writes=None, outbox=None), vault=None
                ),
            ),
        )
        handler = build_handlers(context, [WEBHOOK_DELIVERY_JOB])[WEBHOOK_DELIVERY_JOB]
        now = datetime.now(UTC)
        job = JobRow(
            id=1,
            type=WEBHOOK_DELIVERY_JOB,
            domain_id=None,
            payload={},
            priority=0,
            attempts=1,
            run_after=now,
            locked_by="test-worker",
            created_at=now,
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(TimeoutError, match=r"webhooks\.lease_s \(0\.3s\)"):
            await asyncio.wait_for(handler(job), 5)
        assert loop.time() - started < 2
