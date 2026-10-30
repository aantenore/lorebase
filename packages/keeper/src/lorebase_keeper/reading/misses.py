"""Records the phrases `POST /resolve` matched to nothing, off the request path (design 8.10).

A resolution hands its missed phrases to the recorder without waiting; the recorder adds them
up in memory per Domain, branch and phrase (misses, caller digests, first and last seen) and a
background task writes the totals every `search.resolve.misses.flush_interval_s` in one
transaction per Domain (`lorebase_store.resolve_misses.record_misses`), so a Domain whose
write fails loses only its own misses. At most `queue_size` distinct phrases wait in memory,
and one caller adds at most `max_new_per_caller` of them per interval; a new phrase beyond is
dropped, and so is a Domain's batch whose write fails (`lorebase.resolve.misses.dropped` by
reason). Like the agent trace, nothing is retried: the
counts feed suggestions for review, not an audit.

`missed_phrases` decides what a resolution missed: each run of consecutive words no candidate
covers (split into runs of at most `max_phrase_words`), and with `record_below_score` the
phrases whose best candidate scores below it, kept when they are within the
`store.resolve_misses` length bounds and not an ignored word.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from opentelemetry import metrics
from opentelemetry.metrics import MeterProvider
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.infer.phrases import normalize
from lorebase_keeper.reading.settings import ResolveMissRecording
from lorebase_store.resolve_misses import (
    MissSighting,
    caller_digest,
    phrase_allowed,
    record_misses,
)
from lorebase_store.settings import ResolveMissSettings

logger = logging.getLogger("lorebase.keeper.resolve_misses")


def unknown_runs(words: Sequence[str], covered: Iterable[int], max_words: int) -> list[str]:
    """The runs of consecutive words outside `covered`, each split into runs of at most
    `max_words` words, in text order and without repeats."""
    taken = set(covered)
    runs: list[list[str]] = [[]]
    for position, word in enumerate(words):
        if position in taken:
            runs.append([])
        else:
            runs[-1].append(word)
    found: list[str] = []
    for run in runs:
        for start in range(0, len(run), max(max_words, 1)):
            part = " ".join(run[start : start + max_words])
            if part:
                found.append(part)
    return list(dict.fromkeys(found))


def missed_phrases(
    unknown: Sequence[str],
    weak: Sequence[str],
    settings: ResolveMissSettings,
) -> list[str]:
    """The phrases a resolution records: `unknown` runs and `weak` phrases (matched only
    below `record_below_score`) within the length bounds and not ignored words."""
    ignored = [normalize(w) for w in settings.ignore_words]
    return [
        phrase
        for phrase in dict.fromkeys([*unknown, *weak])
        if phrase_allowed(phrase, settings, ignored)
    ]


@dataclass
class _Pending:
    misses: int
    callers: set[str]
    first: datetime
    last: datetime


@dataclass
class MissRecorder:
    """Aggregates and writes resolution misses (see the module doc)."""

    sessions: async_sessionmaker[AsyncSession]
    store: ResolveMissSettings
    settings: ResolveMissRecording
    meter_provider: MeterProvider | None = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    _pending: dict[tuple[str, str, str], _Pending] = field(default_factory=dict, init=False)
    _new: dict[tuple[str, str], int] = field(default_factory=dict, init=False)
    """New phrases per (Domain, caller) since the last write."""
    _writer: asyncio.Task[None] | None = field(default=None, init=False)
    _writing: asyncio.Task[None] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        meter = (self.meter_provider or metrics.get_meter_provider()).get_meter("lorebase.keeper")
        self._recorded = meter.create_counter(
            "lorebase.resolve.misses.recorded",
            unit="{phrase}",
            description="Resolution misses written (phrases per batch).",
        )
        self._dropped = meter.create_counter(
            "lorebase.resolve.misses.dropped",
            unit="{phrase}",
            description="Resolution misses dropped, by reason (queue_full, domain_full, "
            "caller_limit, write_failed, shutdown).",
        )

    def caller(self, domain_id: str, principal_id: str) -> str:
        """The digest under which `principal_id` is counted in `domain_id`, keyed by
        `store.resolve_misses.caller_key`."""
        key = self.store.caller_key
        if key is None:
            raise RuntimeError("store.resolve_misses.caller_key is not set")
        return caller_digest(domain_id, principal_id, key)

    def offer(self, domain_id: str, branch: str, phrases: Sequence[str], caller: str) -> int:
        """Count `phrases` missed by `caller` (a digest) without waiting; the number taken."""
        if not phrases:
            return 0
        self._ensure_running()
        now = self.clock()
        taken = 0
        for phrase in phrases:
            key = (domain_id, branch, phrase)
            found = self._pending.get(key)
            if found is None:
                if len(self._pending) >= self.settings.queue_size:
                    self._dropped.add(1, {"reason": "queue_full"})
                    continue
                added = self._new.get((domain_id, caller), 0)
                if added >= self.settings.max_new_per_caller:
                    self._dropped.add(1, {"reason": "caller_limit"})
                    continue
                self._new[(domain_id, caller)] = added + 1
                found = self._pending[key] = _Pending(0, set(), now, now)
            found.misses += 1
            if len(found.callers) < self.store.max_callers:
                found.callers.add(caller)
            found.last = now
            taken += 1
        return taken

    def _ensure_running(self) -> None:
        if self._writer is None or self._writer.done():
            self._writer = asyncio.get_running_loop().create_task(
                self._run(), name="resolve-miss-writer"
            )

    def _take(self) -> list[MissSighting]:
        pending, self._pending = self._pending, {}
        self._new = {}
        return [
            MissSighting(
                domain_id=domain,
                branch=branch,
                phrase=phrase,
                misses=entry.misses,
                callers=tuple(sorted(entry.callers)),
                first_seen=entry.first,
                last_seen=entry.last,
            )
            for (domain, branch, phrase), entry in pending.items()
        ]

    async def _write(self, batch: Sequence[MissSighting]) -> None:
        """Each Domain's sightings in a transaction of its own, within `write_timeout_s`."""
        by_domain: dict[str, list[MissSighting]] = {}
        for sighting in batch:
            by_domain.setdefault(sighting.domain_id, []).append(sighting)
        for domain_id, sightings in sorted(by_domain.items()):
            try:
                async with asyncio.timeout(self.settings.write_timeout_s):
                    async with self.sessions() as session, session.begin():
                        stored = await record_misses(session, sightings, settings=self.store)
            except Exception as exc:
                self._dropped.add(len(sightings), {"reason": "write_failed"})
                logger.error(
                    "resolution misses dropped",
                    exc_info=not isinstance(exc, TimeoutError),
                    extra={
                        "domain": domain_id,
                        "phrases": len(sightings),
                        "reason": type(exc).__name__,
                    },
                )
                continue
            self._recorded.add(stored)
            if stored < len(sightings):
                self._dropped.add(len(sightings) - stored, {"reason": "domain_full"})

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.settings.flush_interval_s)
            # Shielded: closing waits for a write in flight instead of cutting it.
            self._writing = asyncio.ensure_future(self._write(self._take()))
            await asyncio.shield(self._writing)

    async def flush(self) -> None:
        """Write what is pending now."""
        await self._write(self._take())

    async def aclose(self) -> None:
        """Stop the writer, let a write in flight finish and write what is pending, within
        one write timeout each; what is left is dropped."""
        if self._writer is not None:
            self._writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._writer
            self._writer = None
        writing, self._writing = self._writing, None
        if writing is not None and not writing.done():
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                async with asyncio.timeout(self.settings.write_timeout_s):
                    await writing
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(self.settings.write_timeout_s):
                await self.flush()
        if self._pending:
            self._dropped.add(len(self._pending), {"reason": "shutdown"})
            self._pending.clear()
