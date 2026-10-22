"""Audit of reads (`keeper.auth.audit.reads`, design 13.6).

`AuditMiddleware` hands every successful read of an authenticated caller to `ReadAuditor`,
which applies the filters and the sampling and queues the record; a background task writes
the queue in batches to the read stream of the audit log (`audit_read_log`, hash chains of its
own: `lorebase_store.auth.audit`). Nothing waits for the database on the request path: when the
queue is full a record is dropped, and a batch that cannot be written is dropped; both are
counted in `lorebase.audit.reads.dropped` by reason and sensitivity. The last tenth of the queue
takes only records of sensitive reads, and a batch holding one is tried twice.

With `on_overflow: reject` nothing is dropped while the service runs: the middleware takes a
place in the queue (`reserve`) before the status line of a read leaves, and refuses the read
with 503 when there is none; a batch that fails is kept and retried every `flush_interval_s`,
and after `unready_after_failures` failures in a row `ReadAuditCheck` turns readiness failed
until a batch is written again. Only a shutdown can then lose queued records (counted).

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
from lorebase_keeper.readiness import CheckResult, CheckStatus
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


class ReadSlot:
    """A place in the queue held for the record of a read being answered (`ReadAuditor.reserve`).
    `fill` queues the record in it, `release` gives it back; the first call wins."""

    def __init__(self, auditor: ReadAuditor) -> None:
        self._auditor: ReadAuditor | None = auditor

    def fill(self, entry: NewAuditEntry) -> None:
        """Queue the record in the held place."""
        auditor, self._auditor = self._auditor, None
        if auditor is not None:
            auditor._reserved -= 1
            auditor._queue.put_nowait(entry)  # cannot fail: the place was counted as taken

    def release(self) -> None:
        """Give the place back (the read is not recorded)."""
        auditor, self._auditor = self._auditor, None
        if auditor is not None:
            auditor._reserved -= 1


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
        self._reserved = 0
        self._in_flight: Sequence[NewAuditEntry] = ()
        self._failures = 0
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
        self._refused = meter.create_counter(
            "lorebase.audit.reads.refused",
            unit="{request}",
            description="Reads refused with 503 because their record found no place in the "
            "queue (on_overflow reject).",
        )
        self._write_failures = meter.create_counter(
            "lorebase.audit.reads.write_failures",
            unit="{batch}",
            description="Failed attempts to write a batch of read records.",
        )

    @property
    def rejects(self) -> bool:
        """Whether reads are refused rather than left unrecorded (`on_overflow: reject`)."""
        return self._settings.on_overflow == "reject"

    @property
    def pending(self) -> int:
        """Records queued and places held, not yet handed to the writer."""
        return self._queue.qsize() + self._reserved

    @property
    def healthy(self) -> bool:
        """False while batches keep failing (`unready_after_failures` attempts in a row)."""
        return self._failures < self._settings.unready_after_failures

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

    def _has_room(self, sensitive: bool) -> bool:
        """The last tenth of the queue is kept for sensitive reads, so a flood of ordinary reads
        cannot crowd them out. Held places (`reserve`) count as taken."""
        taken = self.pending
        reserve = max(1, self._settings.queue_size // SENSITIVE_RESERVE)
        return taken < self._settings.queue_size - (0 if sensitive else reserve)

    def offer(self, entry: NewAuditEntry) -> bool:
        """Queue a record without waiting; false when it was dropped."""
        self._ensure_running()
        sensitive = _sensitive(entry)
        if not self._has_room(sensitive):
            self._dropped.add(1, {"reason": "queue_full", "sensitive": sensitive})
            if sensitive:
                logger.error("sensitive read record dropped: the read audit queue is full")
            return False
        self._queue.put_nowait(entry)
        return True

    def reserve(self, sensitive: bool) -> ReadSlot | None:
        """Hold a place for the record of a read before answering it; None (counted) when the
        queue has none. `sensitive`: the read showed or masked a sensitive value so far."""
        self._ensure_running()
        if not self._has_room(sensitive):
            self._refused.add(1, {"sensitive": sensitive})
            return None
        self._reserved += 1
        return ReadSlot(self)

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
        if self.rejects:
            await self._write_until_done(batch)
            return
        attempts = 2 if any(map(_sensitive, batch)) else 1  # sensitive reads get a retry
        for attempt in range(attempts):
            try:
                async with asyncio.timeout(self._settings.write_timeout_s):
                    await self._sink(batch)
            except Exception as exc:
                self._write_failures.add(1)
                if attempt + 1 < attempts:
                    await asyncio.sleep(self._settings.flush_interval_s)
                    continue
                self._drop_batch(batch, exc)
                return
            self._recorded.add(len(batch))
            return

    async def _write_until_done(self, batch: Sequence[NewAuditEntry]) -> None:
        """Retry a failing batch every flush interval until it is written; past
        `unready_after_failures` failures in a row the auditor reports itself unhealthy."""
        while True:
            try:
                async with asyncio.timeout(self._settings.write_timeout_s):
                    await self._sink(batch)
            except Exception as exc:
                self._write_failures.add(1)
                self._failures += 1
                logger.log(
                    logging.ERROR
                    if self._failures == self._settings.unready_after_failures
                    else logging.WARNING,
                    "read audit batch not written; retrying",
                    exc_info=not isinstance(exc, TimeoutError),
                    extra={
                        "records": len(batch),
                        "failures": self._failures,
                        "reason": type(exc).__name__,
                    },
                )
                await asyncio.sleep(self._settings.flush_interval_s)
                continue
            self._failures = 0
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
            self._in_flight = batch
            await self._write(batch)
            self._in_flight = ()
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
        lost = [*self._in_flight]
        self._in_flight = ()
        while not self._queue.empty():
            lost.append(self._queue.get_nowait())
        for entry in lost:
            self._dropped.add(1, {"reason": "shutdown", "sensitive": _sensitive(entry)})


class ReadAuditCheck:
    """Readiness of the read audit: failed while batches of read records keep failing."""

    name = "read_audit"

    def __init__(self, auditor: ReadAuditor) -> None:
        self._auditor = auditor

    async def __call__(self) -> CheckResult:
        if self._auditor.healthy:
            return CheckResult(CheckStatus.OK)
        return CheckResult(CheckStatus.FAILED, "read records cannot be written")
