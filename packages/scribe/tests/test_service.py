"""A Scribe running materialization jobs sweeps stale path fan-outs next to its job loop."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from lorebase_scribe import service
from lorebase_scribe.service import Scribe
from lorebase_store.jobs import MATERIALIZE_JOB


async def test_the_path_fanout_sweep_does_not_hold_back_the_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_started = asyncio.Event()
    release_sweep = asyncio.Event()
    swept: list[asyncio.Event | None] = []

    async def sweep(sessions: Any, *, runtime: Any, stop: asyncio.Event | None = None) -> None:
        swept.append(stop)
        await release_sweep.wait()

    async def work(stop: asyncio.Event) -> None:
        worker_started.set()
        await stop.wait()

    monkeypatch.setattr(service, "run_path_fanout_sweep", sweep)
    scribe = Scribe(
        engine=None,  # type: ignore[arg-type]
        context=SimpleNamespace(sessions=None, runtime=None),  # type: ignore[arg-type]
        worker=SimpleNamespace(types=[MATERIALIZE_JOB], run=work),  # type: ignore[arg-type]
        scheduler=None,  # type: ignore[arg-type]
        settings=SimpleNamespace(scheduler=SimpleNamespace(enabled=False)),  # type: ignore[arg-type]
    )
    stop = asyncio.Event()
    serving = asyncio.create_task(scribe.serve(stop))
    await asyncio.wait_for(worker_started.wait(), timeout=5)
    assert swept == [stop]  # running, and stopped with the worker
    assert not release_sweep.is_set()
    stop.set()
    release_sweep.set()
    await asyncio.wait_for(serving, timeout=5)
