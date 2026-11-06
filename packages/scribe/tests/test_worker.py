"""The job loop keeps its slots through failures around a job, without Postgres.

The store functions the worker calls are replaced by fakes: an outcome the store refuses once
is written at the second try; refused twice, it counts `unrecorded` and the slot goes on to the
next job; a slot task that ends with an error is started again; the liveness file says how
many slots run.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from lorebase_scribe import worker as worker_module
from lorebase_scribe.liveness import check_liveness
from lorebase_scribe.settings import WorkerSettings
from lorebase_scribe.telemetry import WORKER_SLOTS
from lorebase_scribe.worker import UNRECORDED, Worker
from lorebase_store.jobs import JobRow
from lorebase_store.settings import JobSettings

NOW = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)


def job(job_id: int, **payload: Any) -> JobRow:
    return JobRow(
        id=job_id,
        type="test",
        domain_id="supply",
        payload=payload,
        priority=0,
        attempts=1,
        run_after=NOW,
        locked_by="test-worker",
        created_at=NOW,
    )


class Session:
    """A session that opens and begins without a database."""

    def begin(self) -> Session:
        return self

    async def __aenter__(self) -> Session:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class Store:
    """The queue and the outcomes, in memory; `refusals` is how many outcome writes fail."""

    def __init__(self, *jobs: JobRow, refusals: int = 0) -> None:
        self.queue = list(jobs)
        self.refusals = refusals
        self.finished: list[tuple[int, str | None]] = []
        self.drained = asyncio.Event()

    async def claim_jobs(
        self, session: Any, worker_id: str, *, types: tuple[str, ...], limit: int
    ) -> list[JobRow]:
        if not self.queue:
            self.drained.set()
            return []
        return [self.queue.pop(0)]

    async def finish_job(
        self,
        session: Any,
        row: JobRow,
        *,
        result: Mapping[str, Any] | None,
        error: str | None,
        settings: JobSettings,
    ) -> str:
        if self.refusals:
            self.refusals -= 1
            raise ConnectionError("connection reset")
        self.finished.append((row.id, error))
        return "succeeded" if error is None else "queued"

    async def touch_jobs(self, session: Any, worker_id: str, job_ids: list[int]) -> int:
        return len(job_ids)

    async def release_jobs(self, session: Any, worker_id: str, job_ids: list[int]) -> int:
        return 0


class Counter:
    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []

    def add(self, amount: int, attributes: dict[str, Any]) -> None:
        self.added.append(attributes)


@pytest.fixture
def counter(monkeypatch: pytest.MonkeyPatch) -> Counter:
    counted = Counter()
    monkeypatch.setattr(worker_module, "JOBS", counted)
    return counted


def install(monkeypatch: pytest.MonkeyPatch, store: Store) -> None:
    for name in ("claim_jobs", "finish_job", "touch_jobs", "release_jobs"):
        monkeypatch.setattr(worker_module, name, getattr(store, name))


async def handler(row: JobRow) -> Mapping[str, Any]:
    if row.payload.get("fail"):
        raise RuntimeError("handler failed")
    return {"ran": row.id}


def worker_for(**settings: Any) -> Worker:
    values: dict[str, Any] = {
        "concurrency": 2,
        "poll_interval_s": 0.01,
        "heartbeat_interval_s": 0.05,
        "outcome_retry_delay_s": 0,
        "liveness_interval_s": 0.01,
        **settings,
    }
    return Worker(
        lambda: Session(),  # type: ignore[arg-type, return-value]
        {"test": handler},
        settings=WorkerSettings(**values),
        jobs=JobSettings(),
        worker_id="test-worker",
    )


async def run_until_drained(worker: Worker, store: Store) -> None:
    """Run the loop until the queue is empty, then stop it (the shutdown waits for the jobs
    still running)."""
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))
    await asyncio.wait_for(store.drained.wait(), timeout=5)
    stop.set()
    await asyncio.wait_for(running, timeout=5)


async def test_an_outcome_refused_once_is_written_at_the_second_try(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = Store(job(1), job(2), refusals=1)
    install(monkeypatch, store)
    caplog.set_level(logging.WARNING, logger="lorebase.scribe.worker")

    await run_until_drained(worker_for(), store)

    assert sorted(store.finished) == [(1, None), (2, None)]
    assert "recording the job outcome failed; trying once more" in caplog.text
    assert "not recorded" not in caplog.text


async def test_an_outcome_refused_twice_counts_unrecorded_and_the_slot_goes_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, counter: Counter
) -> None:
    store = Store(job(1), job(2), refusals=2)
    install(monkeypatch, store)
    caplog.set_level(logging.ERROR, logger="lorebase.scribe.worker")
    worker = worker_for(concurrency=1)

    await run_until_drained(worker, store)

    assert store.finished == [(2, None)]  # the second job ran on the same, only, slot
    assert [a["status"] for a in counter.added] == [UNRECORDED, "succeeded"]
    assert "job outcome not recorded" in caplog.text
    assert worker.running == ()  # no longer heartbeated: the stale-lock reclaim takes it


async def test_a_failing_handler_records_the_error(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(job(1, fail=True))
    install(monkeypatch, store)

    status = await worker_for().execute(job(1, fail=True))

    assert status == "queued"
    assert store.finished == [(1, "RuntimeError: handler failed")]


async def test_a_slot_that_ends_with_an_error_is_started_again(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = Store(job(1))
    install(monkeypatch, store)
    caplog.set_level(logging.ERROR, logger="lorebase.scribe.worker")
    worker = worker_for(concurrency=1)
    pause = worker._pause
    broken = asyncio.Event()

    async def pause_once_broken(stop: asyncio.Event, seconds: float) -> None:
        # The first idle pause of the slot (the heartbeat loop pauses for longer) fails.
        if seconds == 0.01 and not broken.is_set():
            broken.set()
            raise RuntimeError("the loop itself failed")
        await pause(stop, seconds)

    monkeypatch.setattr(worker, "_pause", pause_once_broken)
    store.queue.clear()  # the first claim finds nothing, the slot pauses and dies there
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))
    await asyncio.wait_for(broken.wait(), timeout=5)
    await asyncio.sleep(0)
    assert worker.live_slots == 0
    assert dict(observed()) == {"live": 0, "lost": 1}

    store.queue.append(job(1))  # the restarted slot picks it up
    store.drained.clear()
    await asyncio.wait_for(store.drained.wait(), timeout=5)
    assert store.finished == [(1, None)]  # the one slot finished it before claiming again
    assert worker.live_slots == 1
    assert "worker slot ended; starting it again" in caplog.text
    assert "the loop itself failed" in caplog.text
    stop.set()
    await asyncio.wait_for(running, timeout=5)
    assert observed() == []


def observed() -> list[tuple[str, float]]:
    return [
        (str(point.attributes["state"]), point.value)
        for point in WORKER_SLOTS.observe()
        if point.attributes is not None
    ]


async def test_the_liveness_file_reports_the_slots_while_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = Store()
    install(monkeypatch, store)
    path = tmp_path / "state" / "liveness.json"
    worker = worker_for(liveness_file=str(path))
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))
    await asyncio.wait_for(store.drained.wait(), timeout=5)
    await asyncio.sleep(0.05)

    state = json.loads(path.read_text())
    assert state["worker"] == "test-worker"
    assert state["slots"] == {"live": 2, "configured": 2}
    assert state["running"] == []
    assert state["store_reached_at"] is not None  # the claims reached the store
    live, reason = check_liveness(path, max_store_age_s=5)
    assert live, reason
    assert reason.startswith("live: 2 of 2 slots live, 0 jobs running")
    assert dict(observed()) == {"live": 2, "lost": 0}

    stop.set()
    await asyncio.wait_for(running, timeout=5)
    assert not path.exists()
    assert not check_liveness(path)[0]
