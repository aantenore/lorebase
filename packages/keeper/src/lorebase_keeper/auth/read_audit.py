"""Audit of reads (`keeper.auth.audit.reads`, design 13.6).

`AuditMiddleware` hands every successful read of an authenticated caller to `ReadAuditor`,
which applies the filters and the sampling and queues the record; a background task writes
the queue in batches to the read stream of the audit log (`audit_read_log`, hash chains of its
own: `lorebase_store.auth.audit`). Nothing waits for the database on the request path: when the
queue is full a record is dropped, and a batch that cannot be written is dropped; both are
counted in `lorebase.audit.reads.dropped` by reason and sensitivity. The last tenth of the queue
takes only records of sensitive reads, and a batch holding one is tried twice.

Code that renders values reports reads of sensitive Attributes with `note_sensitive`
(`shown` to a cleared caller, `masked` otherwise); the middleware collects the notes of the
request (`collecting`) and such a read is recorded whatever the filters and the sampling. The
notes live in a context variable, so rendering code needs no request object; outside a request
they are ignored.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable, Iterable, Iterator, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal

from opentelemetry import metrics
from opentelemetry.metrics import MeterProvider
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_keeper.auth.config import ReadAuditSettings
from lorebase_store.auth import AuditRepository, AuditStream, NewAuditEntry

logger = logging.getLogger("lorebase.keeper.audit")

SensitiveOutcome = Literal["shown", "masked"]
READ_ACTION = "read"
SENSITIVE_RESERVE = 10
"""1/SENSITIVE_RESERVE of the queue only takes records of sensitive reads."""


def _sensitive(entry: NewAuditEntry) -> bool:
    return bool(entry.details.get("sensitive"))


@dataclass
class ReadNotes:
    """What rendering code reported about the current request."""

    sensitive: set[str] = field(default_factory=set)


_NOTES: ContextVar[ReadNotes | None] = ContextVar("lorebase_read_notes", default=None)


def note_sensitive(outcome: SensitiveOutcome) -> None:
    """Report that the current request showed or masked a value of a sensitive Attribute."""
    notes = _NOTES.get()
    if notes is not None:
        notes.sensitive.add(outcome)


def noted_sensitive() -> frozenset[str]:
    """What the current request reported so far (`note_sensitive`)."""
    notes = _NOTES.get()
    return frozenset(notes.sensitive) if notes is not None else frozenset()


def note_all(outcomes: Iterable[str]) -> None:
    """Report outcomes noted earlier (a cached body replays those of its first rendering)."""
    notes = _NOTES.get()
    if notes is not None:
        notes.sensitive.update(outcomes)


@contextlib.contextmanager
def collecting() -> Iterator[ReadNotes]:
    """Collect the notes of the code run inside the block."""
    notes = ReadNotes()
    token = _NOTES.set(notes)
    try:
        yield notes
    finally:
        _NOTES.reset(token)


ReadSink = Callable[[Sequence[NewAuditEntry]], Awaitable[None]]
"""Writes one batch of read records."""


def database_sink(sessions: async_sessionmaker[AsyncSession]) -> ReadSink:
    """A sink appending to `audit_read_log`, one transaction per batch."""

    async def write(entries: Sequence[NewAuditEntry]) -> None:
        async with sessions() as session, session.begin():
            await AuditRepository(session, AuditStream.READS).append_many(entries)

    return write


class ReadAuditor:
    """Filters, samples, queues and writes read records (see the module doc)."""

    def __init__(
        self,
        settings: ReadAuditSettings,
        sink: ReadSink,
        *,
        meter_provider: MeterProvider | None = None,
        sample: Callable[[], float] = random.random,
    ) -> None:
        self._settings = settings
        self._sink = sink
        self._sample = sample
        self._queue: asyncio.Queue[NewAuditEntry] = asyncio.Queue(maxsize=settings.queue_size)
        self._task: asyncio.Task[None] | None = None
        meter = (meter_provider or metrics.get_meter_provider()).get_meter("lorebase.keeper")
        self._recorded = meter.create_counter(
            "lorebase.audit.reads.recorded",
            unit="{entry}",
            description="Read records written to the read stream of the audit log.",
        )
        self._dropped = meter.create_counter(
            "lorebase.audit.reads.dropped",
            unit="{entry}",
            description="Read records dropped, by reason (queue_full, write_failed).",
        )

    def wants(self, principal_type: str | None, kind: str | None, sensitive: bool) -> bool:
        """Whether a read is recorded: always when sensitive, else when it passes the
        filters and the sampling."""
        if sensitive:
            return True
        settings = self._settings
        if settings.principal_types and principal_type not in settings.principal_types:
            return False
        if settings.kinds and kind not in settings.kinds:
            return False
        return settings.sample_rate >= 1 or self._sample() < settings.sample_rate

    def offer(self, entry: NewAuditEntry) -> bool:
        """Queue a record without waiting; false when it was dropped. The last tenth of the
        queue is kept for sensitive reads, so a flood of ordinary reads cannot crowd them out."""
        self._ensure_running()
        sensitive = _sensitive(entry)
        reserve = max(1, self._settings.queue_size // SENSITIVE_RESERVE)
        if not sensitive and self._queue.qsize() >= self._settings.queue_size - reserve:
            self._dropped.add(1, {"reason": "queue_full", "sensitive": False})
            return False
        try:
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            self._dropped.add(1, {"reason": "queue_full", "sensitive": sensitive})
            logger.error("sensitive read record dropped: the read audit queue is full")
            return False
        return True

    def _ensure_running(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name="read-audit-writer"
            )

    async def _next_batch(self) -> list[NewAuditEntry]:
        """Wait for a record, then take what arrives within the flush interval."""
        batch = [await self._queue.get()]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.flush_interval_s
        while len(batch) < self._settings.batch_size:
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

    async def _write(self, batch: Sequence[NewAuditEntry]) -> None:
        attempts = 2 if any(map(_sensitive, batch)) else 1  # sensitive reads get a retry
        for attempt in range(attempts):
            try:
                async with asyncio.timeout(self._settings.write_timeout_s):
                    await self._sink(batch)
            except Exception as exc:
                if attempt + 1 < attempts:
                    await asyncio.sleep(self._settings.flush_interval_s)
                    continue
                self._drop_batch(batch, exc)
                return
            self._recorded.add(len(batch))
            return

    def _drop_batch(self, batch: Sequence[NewAuditEntry], exc: Exception) -> None:
        sensitive = sum(map(_sensitive, batch))
        for flag, count in ((True, sensitive), (False, len(batch) - sensitive)):
            if count:
                self._dropped.add(count, {"reason": "write_failed", "sensitive": flag})
        logger.error(
            "read audit records dropped",
            exc_info=not isinstance(exc, TimeoutError),
            extra={"records": len(batch), "sensitive": sensitive, "reason": type(exc).__name__},
        )

    async def _run(self) -> None:
        while True:
            batch = await self._next_batch()
            await self._write(batch)
            for _ in batch:
                self._queue.task_done()

    async def flush(self) -> None:
        """Wait until every queued record is written or dropped."""
        if self._task is not None and not self._task.done():
            await self._queue.join()

    async def aclose(self) -> None:
        """Write what is queued (within one write timeout), then stop."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(self._settings.write_timeout_s):
                await self.flush()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        while not self._queue.empty():
            entry = self._queue.get_nowait()
            self._dropped.add(1, {"reason": "shutdown", "sensitive": _sensitive(entry)})
