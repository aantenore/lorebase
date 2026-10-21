"""Assembly of a Scribe process: engine, sessions, HTTP client, worker and scheduler."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from lorebase_platform.settings import Settings
from lorebase_scribe.jobs import PERMANENT_JOB_ERRORS, JobContext, build_handlers
from lorebase_scribe.scheduler import Scheduler
from lorebase_scribe.settings import ScribeSettings, scribe_settings
from lorebase_scribe.sources.connectors import ConnectorFactory, default_connector
from lorebase_scribe.worker import Worker
from lorebase_store.db import create_engine
from lorebase_store.jobs import default_worker_id
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import database_settings, store_settings
from lorebase_store.webhooks import webhook_settings

log = logging.getLogger("lorebase.scribe")


@dataclass
class Scribe:
    """A running Scribe: use `open_scribe` to build one."""

    engine: AsyncEngine
    context: JobContext
    worker: Worker
    scheduler: Scheduler
    settings: ScribeSettings

    async def serve(self, stop: asyncio.Event) -> None:
        """Work and schedule until `stop` is set."""
        tasks = [asyncio.create_task(self.worker.run(stop), name="scribe-worker")]
        if self.settings.scheduler.enabled:
            tasks.append(asyncio.create_task(self.scheduler.run(stop), name="scribe-scheduler"))
        await asyncio.gather(*tasks)


@asynccontextmanager
async def open_scribe(
    settings: Settings,
    *,
    url: str | None = None,
    worker_id: str | None = None,
    connectors: ConnectorFactory = default_connector,
) -> AsyncIterator[Scribe]:
    """Build a Scribe from loaded settings (`url` overrides `database.dsn`); disposes of the
    engine and the HTTP client on exit."""
    scribe = scribe_settings(settings)
    runtime = StoreRuntime.from_settings(store_settings(settings))
    engine = create_engine(
        database_settings(settings),
        url=url,
        statement_timeout_ms=scribe.worker.statement_timeout_ms,
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    worker_name = worker_id or default_worker_id()
    try:
        async with httpx.AsyncClient(trust_env=False) as http:
            context = JobContext(
                engine=engine,
                sessions=sessions,
                runtime=runtime,
                settings=scribe,
                webhooks=webhook_settings(settings),
                http=http,
                worker_id=worker_name,
                connectors=connectors,
            )
            worker = Worker(
                sessions,
                build_handlers(context, scribe.worker.types),
                settings=scribe.worker,
                jobs=runtime.settings.jobs,
                worker_id=worker_name,
                permanent=PERMANENT_JOB_ERRORS,
            )
            scheduler = Scheduler(
                engine,
                sessions,
                settings=scribe.scheduler,
                jobs=runtime.settings.jobs,
                writes=runtime.settings.writes,
            )
            yield Scribe(engine, context, worker, scheduler, scribe)
    finally:
        await engine.dispose()
