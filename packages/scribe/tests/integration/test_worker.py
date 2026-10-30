"""The worker loop, the job handlers and the scheduler against Postgres."""

from __future__ import annotations

import asyncio
import dataclasses
import shutil
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_scribe import scheduler as scheduler_module
from lorebase_scribe.jobs import (
    AUDIT_VERIFY_JOB,
    EXTRACTION_JOB,
    WEBHOOK_DELIVERY_JOB,
    JobContext,
    build_handlers,
)
from lorebase_scribe.scheduler import Scheduler
from lorebase_scribe.settings import SchedulerSettings, ScribeSettings, WorkerSettings
from lorebase_scribe.sources import PERMANENT_ERRORS
from lorebase_scribe.worker import Worker
from lorebase_store.auth import AuditRepository, NewAuditEntry
from lorebase_store.gc import enqueue_gc
from lorebase_store.jobs import (
    INDEX_JOB,
    SOURCE_SYNC_JOB,
    JobRow,
    enqueue_job,
    requeue_stale,
    source_sync_payload,
)
from lorebase_store.repositories import scope_to_domain
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import JobSettings
from lorebase_store.webhooks import WebhookRepository, WebhookSettings, signing_key
from lorebase_store.webhooks.keys import StoredKey

from .support import (
    DOMAIN,
    EXAMPLE,
    Clock,
    commit_on,
    fetch,
    load_domain,
    record_change,
    rows,
    scalar,
)
from .support import supply_schema as example_schema

pytestmark = pytest.mark.integration


@dataclass
class Received:
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class Receiver:
    url: str
    requests: list[Received] = field(default_factory=list)


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    received: list[Received] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            received.append(Received(self.path, dict(self.headers), self.rfile.read(length)))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Receiver(f"http://127.0.0.1:{server.server_address[1]}", received)
    finally:
        server.shutdown()


def worker_for(
    context: JobContext, sessions: async_sessionmaker[AsyncSession], **settings: Any
) -> Worker:
    return Worker(
        sessions,
        build_handlers(context),
        settings=WorkerSettings(**settings),
        jobs=JobSettings(retry_delay_seconds=0),
        worker_id="test-worker",
        permanent=PERMANENT_ERRORS,
    )


@pytest.fixture
async def context(
    engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession], runtime: StoreRuntime
) -> Any:
    async with httpx.AsyncClient(trust_env=False) as http:
        yield JobContext(
            engine=engine,
            sessions=sessions,
            runtime=runtime,
            settings=ScribeSettings(),
            webhooks=WebhookSettings(
                allowed_schemes=("http", "https"),
                allow_private_targets=True,
            ),
            http=http,
            worker_id="test-worker",
        )


async def job_state(sessions: async_sessionmaker[AsyncSession], job_id: int) -> tuple[Any, ...]:
    (row,) = await fetch(
        sessions, "SELECT status, attempts, result FROM jobs WHERE id = :i", i=job_id
    )
    return tuple(row)


async def enqueue(sessions: async_sessionmaker[AsyncSession], job_type: str, **kwargs: Any) -> int:
    async with sessions() as session, session.begin():
        return await enqueue_job(session, job_type, **kwargs)


async def test_worker_runs_a_gc_job(
    context: JobContext,
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
) -> None:
    await load_domain(sessions, runtime, clock, example_schema())
    async with sessions() as session, session.begin():
        job_id = await enqueue_gc(session, DOMAIN, dry_run=True)
    assert await worker_for(context, sessions).run_once() == 1
    status, attempts, result = await job_state(sessions, job_id)
    assert (status, attempts) == ("succeeded", 1)
    assert result["dry_run"] is True


async def test_worker_runs_a_source_sync_job(
    context: JobContext,
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await load_domain(sessions, runtime, clock, example_schema())
    path = tmp_path / "suppliers.csv"
    shutil.copy(EXAMPLE / "data" / "supplier.csv", path)
    monkeypatch.setenv("SUPPLY_APPROVED_SUPPLIERS_CSV", str(path))
    settings = ScribeSettings.model_validate(
        {
            "sources": {
                "allowed_connections": ["secret://env/SUPPLY_APPROVED_SUPPLIERS_CSV"],
                "csv": {"allowed_roots": [str(tmp_path)]},
            }
        }
    )
    job_id = await enqueue(
        sessions,
        SOURCE_SYNC_JOB,
        domain_id=DOMAIN,
        payload=source_sync_payload("approved.suppliers"),
    )
    worker = worker_for(dataclasses.replace(context, settings=settings, clock=clock), sessions)
    # only the sync: it queues the index builds of its overlay proposal branch as further jobs
    assert await worker.run_once(limit=1) == 1
    status, attempts, result = await job_state(sessions, job_id)
    assert (status, attempts) == ("succeeded", 1)
    assert result["counts"]["added"] == 30
    assert result["proposal_status"] == "merged"


async def test_permanent_failures_are_not_retried(
    context: JobContext, sessions: async_sessionmaker[AsyncSession]
) -> None:
    extraction = await enqueue(sessions, EXTRACTION_JOB, domain_id=DOMAIN)
    sync = await enqueue(
        sessions, SOURCE_SYNC_JOB, domain_id="nowhere", payload=source_sync_payload("x")
    )
    assert await worker_for(context, sessions).run_once() == 2
    status, attempts, result = await job_state(sessions, extraction)
    assert (status, attempts) == ("failed", 1)
    assert "needs `documents`" in result["error"]
    status, attempts, result = await job_state(sessions, sync)
    assert (status, attempts) == ("failed", 1)
    assert result["error"].startswith("SourceNotFoundError")


async def test_concurrent_slots_heartbeat_and_graceful_shutdown(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    started = asyncio.Event()
    finished: list[int] = []

    async def slow(job: JobRow) -> Mapping[str, Any]:
        if job.payload.get("block"):
            started.set()
            await asyncio.sleep(30)
        finished.append(job.id)
        return {}

    ids = [await enqueue(sessions, "test", payload={"n": n}) for n in range(3)]
    blocked = await enqueue(sessions, "test", payload={"block": True}, priority=-1)
    worker = Worker(
        sessions,
        {"test": slow},
        settings=WorkerSettings(
            concurrency=2, poll_interval_s=0.05, heartbeat_interval_s=0.1, shutdown_grace_s=0.2
        ),
        jobs=JobSettings(),
        worker_id="slots",
    )
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))
    await asyncio.wait_for(started.wait(), timeout=10)
    before = await scalar(sessions, "SELECT locked_at FROM jobs WHERE id = :i", i=blocked)
    await asyncio.sleep(0.3)
    after = await scalar(sessions, "SELECT locked_at FROM jobs WHERE id = :i", i=blocked)
    assert after > before  # the heartbeat refreshed the lock
    stop.set()
    await asyncio.wait_for(running, timeout=10)
    assert sorted(finished) == ids
    status, attempts, _ = await job_state(sessions, blocked)
    assert (status, attempts) == ("queued", 0)  # released without counting the attempt


async def reclaim(sessions: async_sessionmaker[AsyncSession], jobs: JobSettings) -> int:
    async with sessions() as session, session.begin():
        return await requeue_stale(session, settings=jobs)


async def test_a_worker_without_heartbeat_loses_its_job_to_another_worker(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    jobs = JobSettings(stale_after_seconds=0.5, retry_delay_seconds=0)
    settings = WorkerSettings(poll_interval_s=0.05, heartbeat_interval_s=0.1)
    runs: list[str] = []
    started, resume = asyncio.Event(), asyncio.Event()

    def handler(name: str) -> Any:
        async def run(job: JobRow) -> Mapping[str, Any]:
            runs.append(name)
            if name == "dead":
                started.set()
                await resume.wait()
            return {"by": name}

        return run

    job_id = await enqueue(sessions, "test")
    dead = Worker(
        sessions, {"test": handler("dead")}, settings=settings, jobs=jobs, worker_id="dead"
    )
    live = Worker(
        sessions, {"test": handler("live")}, settings=settings, jobs=jobs, worker_id="live"
    )
    # The dead worker claimed the job and froze: its handler runs but no heartbeat is sent.
    claimed = await dead.claim()
    assert claimed is not None
    assert claimed.id == job_id
    frozen = asyncio.create_task(dead.execute(claimed))
    await asyncio.wait_for(started.wait(), timeout=10)
    assert await reclaim(sessions, jobs) == 0  # within the threshold the job stays its own
    assert await live.run_once() == 0
    await asyncio.sleep(0.6)
    assert await reclaim(sessions, jobs) == 1
    assert await live.run_once() == 1
    status, attempts, result = await job_state(sessions, job_id)
    assert (status, attempts, result) == ("succeeded", 2, {"by": "live"})
    # The frozen worker wakes up: it cannot overwrite the outcome of the run that replaced it.
    resume.set()
    assert await asyncio.wait_for(frozen, timeout=10) == "lost"
    assert await job_state(sessions, job_id) == ("succeeded", 2, {"by": "live"})
    assert runs == ["dead", "live"]


async def test_a_job_that_keeps_losing_its_worker_fails_after_max_attempts(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    jobs = JobSettings(stale_after_seconds=0.2, max_attempts=2)
    job_id = await enqueue(sessions, "test")
    worker = Worker(
        sessions,
        {"test": lambda job: asyncio.sleep(0, {})},
        settings=WorkerSettings(poll_interval_s=0.05, heartbeat_interval_s=0.1),
        jobs=jobs,
        worker_id="crashing",
    )
    for expected in ("queued", "failed"):
        assert await worker.claim() is not None  # claimed, then the worker dies
        await asyncio.sleep(0.3)
        assert await reclaim(sessions, jobs) == 1
        status, attempts, result = await job_state(sessions, job_id)
        assert status == expected
    assert attempts == 2
    assert result["error"].startswith("lost: no heartbeat")


async def test_heartbeats_keep_a_long_job_with_its_worker(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    jobs = JobSettings(stale_after_seconds=0.5)
    runs: list[int] = []
    started, ended = asyncio.Event(), asyncio.Event()

    async def long(job: JobRow) -> Mapping[str, Any]:
        runs.append(job.id)
        started.set()
        await asyncio.sleep(1.5)
        ended.set()
        return {}

    job_id = await enqueue(sessions, "test")
    worker = Worker(
        sessions,
        {"test": long},
        settings=WorkerSettings(poll_interval_s=0.05, heartbeat_interval_s=0.1),
        jobs=jobs,
        worker_id="alive",
    )
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))
    await asyncio.wait_for(started.wait(), timeout=10)
    for _ in range(8):  # three times the threshold: the heartbeat keeps the job
        await asyncio.sleep(0.2)
        assert await reclaim(sessions, jobs) == 0
    await asyncio.wait_for(ended.wait(), timeout=10)
    stop.set()  # the slot records the outcome within the shutdown grace
    await asyncio.wait_for(running, timeout=10)
    assert (await job_state(sessions, job_id))[:2] == ("succeeded", 1)
    assert runs == [job_id]  # run once


async def test_shutdown_releases_a_job_whose_handler_ignores_cancellation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    started, done = asyncio.Event(), asyncio.Event()

    async def stubborn(job: JobRow) -> Mapping[str, Any]:
        started.set()
        while not done.is_set():
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                continue
        return {}

    job_id = await enqueue(sessions, "test")
    worker = Worker(
        sessions,
        {"test": stubborn},
        settings=WorkerSettings(
            poll_interval_s=0.05,
            heartbeat_interval_s=0.1,
            shutdown_grace_s=0.1,
            shutdown_cancel_s=0.1,
        ),
        jobs=JobSettings(),
        worker_id="stubborn",
    )
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))
    await asyncio.wait_for(started.wait(), timeout=10)
    stop.set()
    await asyncio.wait_for(running, timeout=10)
    assert (await job_state(sessions, job_id))[:2] == ("queued", 0)
    assert worker.running == ()
    done.set()
    await asyncio.sleep(0.2)  # the abandoned slot finds its job released and stops


async def test_webhook_delivery_job_end_to_end(
    context: JobContext,
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    receiver: Receiver,
) -> None:
    schema = example_schema()
    await load_domain(sessions, runtime, clock, schema, kinds={"Supplier": "supplier.csv"})
    async with sessions() as session, session.begin():
        drain = await enqueue_job(session, WEBHOOK_DELIVERY_JOB, unique=True)
    worker = worker_for(context, sessions)
    await worker.run_once()  # hands off the events of the loading commits
    assert (await job_state(sessions, drain))[0] == "succeeded"
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await WebhookRepository(session, DOMAIN).add(
            url=f"{receiver.url}/hook",
            description="tests",
            event_types=["revision.committed"],
            branches=None,
            signing_key=StoredKey(signing_key("secret"), None),
            created_by="usr_admin",
        )
    kind = schema.kind_by_api_name("Supplier")
    assert kind is not None
    row = next(r for r in rows("supplier.csv") if r["supplier_code"] == "S-1002")
    await commit_on(sessions, runtime, clock, [record_change(kind, {**row, "risk_score": "90"})])
    job_id = await enqueue(sessions, WEBHOOK_DELIVERY_JOB, unique=True)
    assert await worker.run_once() == 1
    status, _, result = await job_state(sessions, job_id)
    assert status == "succeeded"
    assert (result["queued"], result["delivered"]) == (1, 1)
    (request,) = receiver.requests
    assert request.path == "/hook"
    assert b"S-1002" in request.body


async def wait_status(
    sessions: async_sessionmaker[AsyncSession], job_id: int, *statuses: str
) -> tuple[Any, ...]:
    while True:
        state = await job_state(sessions, job_id)
        if state[0] in statuses:
            return state
        await asyncio.sleep(0.05)


async def test_a_silent_receiver_never_holds_the_worker(
    context: JobContext,
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
) -> None:
    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(3600)  # accepts the connection and never answers

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    schema = example_schema()
    await load_domain(sessions, runtime, clock, schema, kinds={"Supplier": "supplier.csv"})
    worker = worker_for(context, sessions)
    await enqueue(sessions, WEBHOOK_DELIVERY_JOB, unique=True)
    await worker.run_once()  # hands off the events of the loading commits
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await WebhookRepository(session, DOMAIN).add(
            url=f"http://127.0.0.1:{port}/hook",
            description="gone silent",
            event_types=["revision.committed"],
            branches=None,
            signing_key=StoredKey(signing_key("secret"), None),
            created_by="usr_admin",
        )
    kind = schema.kind_by_api_name("Supplier")
    assert kind is not None
    row = next(r for r in rows("supplier.csv") if r["supplier_code"] == "S-1002")
    await commit_on(sessions, runtime, clock, [record_change(kind, {**row, "risk_score": "90"})])

    webhooks = context.webhooks.model_copy(
        update={"timeout_s": 2.0, "connect_timeout_s": 1.0, "pass_timeout_s": 2.0, "lease_s": 10}
    )
    worker = worker_for(
        dataclasses.replace(context, webhooks=webhooks),
        sessions,
        concurrency=2,
        poll_interval_s=0.05,
        heartbeat_interval_s=0.1,
    )
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))
    try:
        delivery = await enqueue(sessions, WEBHOOK_DELIVERY_JOB, unique=True)
        await asyncio.wait_for(wait_status(sessions, delivery, "running"), 10)
        # the other slot keeps working while the pass waits on the silent receiver
        other = await enqueue(sessions, AUDIT_VERIFY_JOB, unique=True)
        assert (await asyncio.wait_for(wait_status(sessions, other, "succeeded"), 10))[0]
        assert (await job_state(sessions, delivery))[0] == "running"
        status, _, result = await asyncio.wait_for(
            wait_status(sessions, delivery, "succeeded", "queued", "failed"), 10
        )
    finally:
        stop.set()
        await asyncio.wait_for(running, 10)
        server.close()
    assert status == "succeeded"
    assert (result["attempted"], result["retrying"], result["timed_out"]) == (1, 1, 1)
    (error,) = await fetch(sessions, "SELECT last_error FROM webhook_deliveries")
    assert error[0] == "timed out after 2s"


async def test_worker_runs_a_store_index_job(
    context: JobContext,
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
) -> None:
    await load_domain(sessions, runtime, clock, example_schema())
    name = "mat_records_attr_unplanned"
    payload = {"action": "create", "name": name, "branch": "main", "kind": "k_none"}
    job_id = await enqueue(sessions, INDEX_JOB, domain_id=DOMAIN, payload=payload)
    assert await worker_for(context, sessions).run_once() == 1
    status, _, result = await job_state(sessions, job_id)
    assert status == "succeeded"
    assert result["status"] == "cancelled"  # the branch does not plan this index
    assert result["name"] == name


async def test_audit_verify_job_reports_the_first_broken_entry(
    context: JobContext, sessions: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    seqs = []
    for n in range(3):
        async with sessions() as session, session.begin():
            entry = NewAuditEntry(actor="usr_1", action=f"test.{n}", details={"n": n})
            seqs.append((await AuditRepository(session).append(entry)).seq)
    worker = worker_for(context, sessions)
    intact = await enqueue(sessions, AUDIT_VERIFY_JOB, unique=True)
    assert await worker.run_once() == 1
    status, _, result = await job_state(sessions, intact)
    assert status == "succeeded"
    assert result == {
        "intact": True,
        "checked": 3,
        "chains": 1,
        "last_seq": seqs[2],
        "reads": {"intact": True, "checked": 0, "chains": 0, "last_seq": None},
    }

    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE audit_log SET action = 'test.forged' WHERE seq = :seq"), {"seq": seqs[1]}
        )
    broken = await enqueue(sessions, AUDIT_VERIFY_JOB, unique=True)
    assert await worker.run_once() == 1
    status, _, result = await job_state(sessions, broken)
    assert status == "succeeded"
    assert result["intact"] is False
    assert result["first_broken"] == {
        "seq": seqs[1],
        "domain_id": None,
        "reason": "entry content changed",
    }


class Stepper:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Recorder:
    """Stands in for a counter: keeps every `add`."""

    calls: list[tuple[int, dict[str, str]]] = field(default_factory=list)

    def add(self, amount: int, attributes: dict[str, str]) -> None:
        self.calls.append((amount, attributes))


async def test_scheduler_leader_election_and_schedules(
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await load_domain(sessions, runtime, clock, example_schema())
    gc_scheduled = Recorder()
    monkeypatch.setattr(scheduler_module, "GC_SCHEDULED", gc_scheduled)
    time = Stepper(datetime(2026, 10, 21, 1, 0, tzinfo=UTC))
    settings = SchedulerSettings(
        webhook_interval_s=10,
        embeddings_interval_s=0,
        value_index_interval_s=0,
        coverage_interval_s=0,
        audit_verify_interval_s=86400,
    )

    def scheduler() -> Scheduler:
        return Scheduler(
            engine,
            sessions,
            settings=settings,
            jobs=runtime.settings.jobs,
            writes=runtime.settings.writes,
            clock=time,
        )

    first, second = scheduler(), scheduler()
    try:
        queued = await first.tick()
        assert first.is_leader
        assert await second.tick() == []
        assert not second.is_leader
        # The leader queued the interval jobs; cron jobs whose firing precedes the
        # leadership wait for their next firing.
        types = await fetch(sessions, "SELECT type FROM jobs ORDER BY id")
        assert [t for (t,) in types] == [WEBHOOK_DELIVERY_JOB, AUDIT_VERIFY_JOB]
        assert len(queued) == 2
        # The gc counter starts at 0 with the schedule's mode before any firing.
        assert gc_scheduled.calls == [(0, {"dry_run": "false"})]

        time.now += timedelta(hours=2, minutes=30)  # 03:30: after gc (03:00) and erp (02:00)
        await first.tick()
        assert await second.tick() == []
        scheduled = await fetch(
            sessions,
            "SELECT type, domain_id, payload ->> 'source', run_after FROM jobs "
            "WHERE domain_id IS NOT NULL ORDER BY type",
        )
        erp = example_schema().source_by_api_name("erp.purchase_orders")
        assert erp is not None
        assert [tuple(r) for r in scheduled] == [
            ("gc", DOMAIN, None, datetime(2026, 10, 21, 3, 0, tzinfo=UTC)),
            ("source_sync", DOMAIN, erp.id, datetime(2026, 10, 21, 2, 0, tzinfo=UTC)),
        ]
        assert gc_scheduled.calls[-1] == (1, {"dry_run": "false"})
        await first.tick()  # nothing new is due
        assert await scalar(sessions, "SELECT count(*) FROM jobs WHERE domain_id IS NOT NULL") == 2

        await first.release()
        assert not first.is_leader
        await second.tick()
        assert second.is_leader
    finally:
        await first.release()
        await second.release()
