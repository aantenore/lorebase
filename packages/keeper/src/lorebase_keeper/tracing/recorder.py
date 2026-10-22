"""Writes trace steps off the request path, and deletes expired ones.

Like the read auditor (`auth.read_audit`), the recorder queues steps without waiting and a
background task writes them in batches; a full queue or a failed batch drops steps, counted
in `lorebase.trace.steps.dropped` by reason. A second task deletes steps older than the
retention period every `prune_interval_s`. Tracing is debugging, not compliance: nothing is
retried.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta

from opentelemetry import metrics
from opentelemetry.metrics import MeterProvider
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_keeper.tracing.config import TraceSettings
from lorebase_store.tracing import NewTraceStep, TraceRepository

logger = logging.getLogger("lorebase.keeper.trace")


class TraceSink:
    """Where steps go: written in batches, and pruned."""

    def __init__(
        self,
        write: Callable[[Sequence[NewTraceStep]], Awaitable[int]],
        prune: Callable[[datetime], Awaitable[int]],
    ) -> None:
        self.write = write
        self.prune = prune


def database_sink(sessions: async_sessionmaker[AsyncSession], settings: TraceSettings) -> TraceSink:
    """A sink writing to `agent_trace_steps`, one transaction per batch."""

    async def write(steps: Sequence[NewTraceStep]) -> int:
        async with sessions() as session, session.begin():
            return await TraceRepository(session).append_many(
                steps,
                max_per_session=settings.max_steps_per_session,
                inferred_idle_s=settings.inferred_session_idle_s,
            )

    async def prune(before: datetime) -> int:
        async with sessions() as session, session.begin():
            return await TraceRepository(session).prune(before, limit=settings.prune_batch)

    return TraceSink(write, prune)


class TraceRecorder:
    """Queues, writes and prunes trace steps (see the module doc)."""

    def __init__(
        self,
        settings: TraceSettings,
        sink: TraceSink,
        *,
        meter_provider: MeterProvider | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings
        self._sink = sink
        self._clock = clock
        self._queue: asyncio.Queue[NewTraceStep] = asyncio.Queue(maxsize=settings.queue_size)
        self._writer: asyncio.Task[None] | None = None
        self._pruner: asyncio.Task[None] | None = None
        meter = (meter_provider or metrics.get_meter_provider()).get_meter("lorebase.keeper")
        self._recorded = meter.create_counter(
            "lorebase.trace.steps.recorded",
            unit="{step}",
            description="Agent trace steps written.",
        )
        self._dropped = meter.create_counter(
            "lorebase.trace.steps.dropped",
            unit="{step}",
            description="Agent trace steps dropped, by reason (queue_full, write_failed, "
            "session_full, shutdown).",
        )

    def offer(self, step: NewTraceStep) -> bool:
        """Queue a step without waiting; false when it was dropped."""
        self._ensure_running()
        try:
            self._queue.put_nowait(step)
        except asyncio.QueueFull:
            self._dropped.add(1, {"reason": "queue_full"})
            return False
        return True

    def _ensure_running(self) -> None:
        loop = asyncio.get_running_loop()
        if self._writer is None or self._writer.done():
            self._writer = loop.create_task(self._run(), name="trace-writer")
        if self._pruner is None or self._pruner.done():
            self._pruner = loop.create_task(self._prune_loop(), name="trace-pruner")

    async def _next_batch(self) -> list[NewTraceStep]:
        batch = [await self._queue.get()]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.flush_interval_s
        while len(batch) < self.settings.batch_size:
            try:
                batch.append(self._queue.get_nowait())
                continue
            except asyncio.QueueEmpty:
                pass
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                async with asyncio.timeout(remaining):
                    batch.append(await self._queue.get())
            except TimeoutError:
                break
        return batch

    async def _write(self, batch: Sequence[NewTraceStep]) -> None:
        try:
            async with asyncio.timeout(self.settings.write_timeout_s):
                written = await self._sink.write(batch)
        except Exception as exc:
            self._dropped.add(len(batch), {"reason": "write_failed"})
            logger.error(
                "agent trace steps dropped",
                exc_info=not isinstance(exc, TimeoutError),
                extra={"steps": len(batch), "reason": type(exc).__name__},
            )
            return
        self._recorded.add(written)
        if written < len(batch):
            self._dropped.add(len(batch) - written, {"reason": "session_full"})

    async def _run(self) -> None:
        while True:
            batch = await self._next_batch()
            await self._write(batch)
            for _ in batch:
                self._queue.task_done()

    async def prune_once(self) -> int:
        """Delete expired steps (one batch); the number deleted."""
        before = self._clock() - timedelta(days=self.settings.retention_days)
        return await self._sink.prune(before)

    async def _prune_loop(self) -> None:
        while True:
            try:
                async with asyncio.timeout(self.settings.write_timeout_s):
                    await self.prune_once()
            except Exception:
                logger.warning("deleting expired agent trace steps failed", exc_info=True)
            await asyncio.sleep(self.settings.prune_interval_s)

    async def flush(self) -> None:
        """Wait until every queued step is written or dropped."""
        if self._writer is not None and not self._writer.done():
            await self._queue.join()

    async def aclose(self) -> None:
        """Write what is queued (within one write timeout), then stop."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(self.settings.write_timeout_s):
                await self.flush()
        for task in (self._writer, self._pruner):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._writer = self._pruner = None
        while not self._queue.empty():
            self._queue.get_nowait()
            self._dropped.add(1, {"reason": "shutdown"})
