"""Embedding passes (design 9.3): vectors for the Records of Kinds with an embedding template.

A pass covers the materialized branches of one Domain. For each branch:

1. A branch whose schema has no embedding template is skipped without creating the model.
   A branch whose head equals the head of its last complete pass for the model is skipped
   too (`embedding_cursors`); a schema change moves the head, so templates are unchanged.
2. Otherwise its own live rows are read in `(kind, key)` order, page by page: rows whose
   object hash has no vector of the model, and every row of the Kinds whose template
   fingerprint differs from the last pass (or of every Kind with `reindex`).
3. Each row's text is rendered from the Kind's template (`lorebase_core.model.embedding`)
   and hashed. A row whose stored vector has the same text hash is unchanged; a row whose
   text equals one already embedded copies that vector; the remaining texts go to the model
   in batches of `batch_texts`, at most `max_calls_per_second`, each call retried
   `retry.attempts` times. Empty texts get no vector.
4. A complete pass records the head and the fingerprints.

Vectors are keyed by object hash, so the rows a branch shares with another one (an overlay's
inherited rows, the unchanged rows of a copy) are embedded once. A job reads at most
`max_records_per_job` rows; the rest of the pass is left to a continuation that resumes after
the last row read. One pass per Domain runs at a time (a session advisory lock); a second one
returns at once with `busy`.

Model checks: the model's dimension must equal `store.embeddings.dims` (the dimension of the
vector index Keeper searches), and the first use of the model embeds a probe text whose
vector size must match. Either mismatch fails the job without retries.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import Schema
from lorebase_core.model.embedding import RENDERING_VERSION, KindTemplate, TemplateError
from lorebase_core.model.embedding import kind_template as resolve_template
from lorebase_inference import (
    DimensionMismatchError,
    EmbeddingModel,
    InferenceError,
    InferenceSettings,
    ProviderUnavailableError,
    UnknownProviderError,
    create_embedding_model,
)
from lorebase_scribe.settings import EmbeddingJobSettings
from lorebase_scribe.telemetry import EMBEDDING_CALL_DURATION, EMBEDDING_RECORDS, tracer
from lorebase_store.embeddings import (
    BranchHead,
    Candidate,
    Cursor,
    NewVector,
    branch_schema,
    candidates,
    lock_key,
    materialized_branches,
    prune_models,
    read_cursor,
    reuse_vectors,
    save_cursor,
    store_vectors,
    text_hash,
)
from lorebase_store.repositories import scope_to_domain

log = logging.getLogger("lorebase.scribe.embeddings")

PROBE_TEXT = "embedding dimension probe"
VERSION_KEY = "_rendering"
"""Fingerprint entry holding `RENDERING_VERSION`, so a new rendering rule reruns passes."""

Sleep = Callable[[float], Awaitable[None]]


class EmbeddingConfigError(Exception):
    """The embedding model does not fit the vector storage; retrying cannot help."""


PERMANENT_ERRORS: tuple[type[Exception], ...] = (
    EmbeddingConfigError,
    DimensionMismatchError,
    ProviderUnavailableError,
    UnknownProviderError,
)
"""Failures of an embedding job that are not retried."""


def utc_now() -> datetime:
    return datetime.now(UTC)


class EmbeddingModels:
    """The process's embedding model: created on first use, checked once, then shared."""

    def __init__(
        self,
        inference: InferenceSettings,
        *,
        dims: int,
        factory: Callable[[], EmbeddingModel] | None = None,
    ) -> None:
        self._inference = inference
        self._dims = dims
        self._factory = factory or (lambda: create_embedding_model(inference))
        self._model: EmbeddingModel | None = None
        self._checked = False
        self._lock = asyncio.Lock()

    @property
    def label(self) -> str:
        """The model's name once created, else the configured model (for reports)."""
        return self._inference.embeddings.model if self._model is None else self._model.name

    def instance(self) -> EmbeddingModel:
        """The model, created (not yet called) on first use; its `name` keys the vectors."""
        if self._model is None:
            model = self._factory()
            if model.dims != self._dims:
                raise EmbeddingConfigError(
                    f"embedding model {model.name!r} has {model.dims} dimensions, "
                    f"store.embeddings.dims is {self._dims}"
                )
            self._model = model
        return self._model

    async def get(self) -> EmbeddingModel:
        """The model, checked once with a probe text before its first real call."""
        async with self._lock:
            model = self.instance()
            if not self._checked:
                probe = await model.embed([PROBE_TEXT])
                if len(probe) != 1 or len(probe[0]) != self._dims:
                    size = len(probe[0]) if probe else 0
                    raise EmbeddingConfigError(
                        f"embedding model {model.name!r} returned {size} dimensions, "
                        f"store.embeddings.dims is {self._dims}"
                    )
                self._checked = True
            return model

    async def aclose(self) -> None:
        close = getattr(self._model, "aclose", None)
        if close is not None:
            await close()


@dataclass
class BranchReport:
    branch: str
    skipped: bool = False
    rows: int = 0
    embedded: int = 0
    reused: int = 0
    unchanged: int = 0
    empty: int = 0
    calls: int = 0
    complete: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "skipped": self.skipped,
            "rows": self.rows,
            "embedded": self.embedded,
            "reused": self.reused,
            "unchanged": self.unchanged,
            "empty": self.empty,
            "calls": self.calls,
            "complete": self.complete,
        }


@dataclass
class EmbeddingReport:
    domain: str
    model: str
    busy: bool = False
    pruned: int = 0
    branches: list[BranchReport] = field(default_factory=list)
    resume: tuple[str, str, str] | None = None
    """Where a continuation resumes: branch, Kind id and key of the last row read."""

    @property
    def complete(self) -> bool:
        return not self.busy and self.resume is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "model": self.model,
            "busy": self.busy,
            "complete": self.complete,
            "pruned": self.pruned,
            "resume": None if self.resume is None else list(self.resume),
            "branches": [b.as_dict() for b in self.branches],
        }


@dataclass
class _Budget:
    rows: int


class _Pacer:
    """At most `rate` calls per second (0: no limit)."""

    def __init__(self, rate: float, sleep: Sleep, clock: Callable[[], float]) -> None:
        self._interval = 1.0 / rate if rate > 0 else 0.0
        self._sleep = sleep
        self._clock = clock
        self._next = 0.0

    async def wait(self) -> None:
        if self._interval <= 0:
            return
        now = self._clock()
        if now < self._next:
            await self._sleep(self._next - now)
            now = self._next
        self._next = now + self._interval


class EmbeddingPass:
    """One embedding pass over a Domain (see the module documentation)."""

    def __init__(
        self,
        engine: AsyncEngine,
        sessions: async_sessionmaker[AsyncSession],
        models: EmbeddingModels,
        settings: EmbeddingJobSettings,
        *,
        dims: int,
        clock: Callable[[], datetime] = utc_now,
        sleep: Sleep = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._sessions = sessions
        self._models = models
        self._settings = settings
        self._dims = dims
        self._clock = clock
        self._sleep = sleep
        self._pacer = _Pacer(settings.max_calls_per_second, sleep, monotonic)

    async def run(
        self,
        domain_id: str,
        *,
        branches: Collection[str] | None = None,
        reindex: bool = False,
        resume: tuple[str, str, str] | None = None,
        prune: bool = False,
    ) -> EmbeddingReport:
        """Run the pass; `prune` then deletes the vectors of other models when it completed."""
        report = EmbeddingReport(domain_id, self._models.label)
        key = lock_key(domain_id)
        with tracer.start_as_current_span(
            "scribe.embeddings", attributes={"lorebase.domain": domain_id}
        ) as span:
            async with self._engine.connect() as connection:
                auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
                locked = (
                    await auto.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
                ).scalar_one()
                if not locked:
                    report.busy = True
                    return report
                try:
                    await self._run(report, domain_id, branches, reindex, resume)
                    if prune and report.complete:
                        # The name vectors are stored under, even when no branch needed the
                        # model in this pass.
                        report.model = self._models.instance().name
                        async with self._sessions() as session, session.begin():
                            await scope_to_domain(session, domain_id)
                            report.pruned = await prune_models(
                                session, domain_id, keep=report.model
                            )
                finally:
                    with contextlib.suppress(Exception):
                        await auto.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            span.set_attribute("lorebase.complete", report.complete)
        return report

    async def _run(
        self,
        report: EmbeddingReport,
        domain_id: str,
        branches: Collection[str] | None,
        reindex: bool,
        resume: tuple[str, str, str] | None,
    ) -> None:
        async with self._sessions() as session, session.begin():
            await scope_to_domain(session, domain_id)
            heads = await materialized_branches(session, domain_id, branches)
        budget = _Budget(self._settings.max_records_per_job)
        for head in heads:
            after: tuple[str, str] | None = None
            if resume is not None:
                if head.name < resume[0]:
                    continue
                if head.name == resume[0]:
                    after = (resume[1], resume[2])
            branch = BranchReport(head.name)
            report.branches.append(branch)
            last = await self._branch(domain_id, head, branch, reindex, after, budget)
            report.model = self._models.label
            if last is not None:
                report.resume = (head.name, *last)
                return

    async def _branch(
        self,
        domain_id: str,
        head: BranchHead,
        report: BranchReport,
        reindex: bool,
        after: tuple[str, str] | None,
        budget: _Budget,
    ) -> tuple[str, str] | None:
        """Embed one branch; returns the last row read when the budget ran out first."""
        async with self._sessions() as session, session.begin():
            await scope_to_domain(session, domain_id)
            schema = await branch_schema(session, domain_id, head.name)
        templates = _templates(schema, domain_id, head.name)
        if not templates:
            report.skipped = report.complete = True
            return None
        model_name = self._models.instance().name
        async with self._sessions() as session, session.begin():
            await scope_to_domain(session, domain_id)
            cursor = await read_cursor(session, domain_id, head.name, model_name)
        if (
            not reindex
            and cursor is not None
            and cursor.head == head.head
            and cursor.fingerprints.get(VERSION_KEY) == str(RENDERING_VERSION)
        ):
            report.skipped = report.complete = True
            return None
        fingerprints = {kind: t.fingerprint for kind, t in templates.items()}
        if reindex:
            rerender: set[str] = set(templates)
        else:
            known = {} if cursor is None else cursor.fingerprints
            rerender = {
                kind
                for kind, fingerprint in fingerprints.items()
                if cursor is not None and known.get(kind) != fingerprint
            }
        while templates:
            if budget.rows <= 0:
                # Resume at the start of the branch when nothing of it was read yet: every
                # (kind id, key) sorts after ("", "").
                return after or ("", "")
            async with self._sessions() as session, session.begin():
                await scope_to_domain(session, domain_id)
                rows = await candidates(
                    session,
                    domain_id,
                    head.name,
                    model=model_name,
                    dims=self._dims,
                    kinds=templates.keys(),
                    rerender=rerender,
                    after=after,
                    limit=min(self._settings.page_rows, budget.rows),
                )
            if not rows:
                break
            budget.rows -= len(rows)
            report.rows += len(rows)
            after = (rows[-1].kind_id, rows[-1].key)
            await self._embed_rows(domain_id, rows, templates, report)
        async with self._sessions() as session, session.begin():
            await scope_to_domain(session, domain_id)
            await save_cursor(
                session,
                domain_id,
                head.name,
                model_name,
                Cursor(head.head, {**fingerprints, VERSION_KEY: str(RENDERING_VERSION)}),
                at=self._clock(),
            )
        report.complete = True
        return None

    async def _embed_rows(
        self,
        domain_id: str,
        rows: Sequence[Candidate],
        templates: dict[str, KindTemplate],
        report: BranchReport,
    ) -> None:
        counts = {"empty": 0, "unchanged": 0, "reused": 0, "embedded": 0}
        pending: dict[bytes, tuple[bytes, str]] = {}
        for row in rows:
            rendered = templates[row.kind_id].render(row.key, row.values)
            rendered = rendered[: self._settings.max_text_chars]
            if not rendered:
                counts["empty"] += 1
                continue
            digest = text_hash(rendered)
            if row.embedded and row.text_hash == digest:
                counts["unchanged"] += 1
                continue
            pending[row.object_hash] = (digest, rendered)
        try:
            if pending:
                calls = await self._embed_pending(domain_id, pending, counts)
                report.calls += calls
        finally:
            for outcome, count in counts.items():
                setattr(report, outcome, getattr(report, outcome) + count)
                if count:
                    EMBEDDING_RECORDS.add(count, {"outcome": outcome})

    async def _embed_pending(
        self, domain_id: str, pending: dict[bytes, tuple[bytes, str]], counts: dict[str, int]
    ) -> int:
        """Vectors for `pending` (object hash -> text hash and text); returns the model calls."""
        model = await self._models.get()
        async with self._sessions() as session, session.begin():
            await scope_to_domain(session, domain_id)
            reused = await reuse_vectors(
                session,
                domain_id,
                model=model.name,
                dims=self._dims,
                wanted=[(h, d) for h, (d, _) in pending.items()],
            )
        counts["reused"] += len(reused)
        texts: dict[bytes, str] = {}
        owners: dict[bytes, list[bytes]] = {}
        for object_hash, (digest, rendered) in pending.items():
            if object_hash not in reused:
                texts[digest] = rendered
                owners.setdefault(digest, []).append(object_hash)
        digests = list(texts)
        calls = 0
        for start in range(0, len(digests), self._settings.batch_texts):
            batch = digests[start : start + self._settings.batch_texts]
            vectors = await self._call(model, [texts[d] for d in batch])
            calls += 1
            new = [
                NewVector(object_hash, digest, vector)
                for digest, vector in zip(batch, vectors, strict=True)
                for object_hash in owners[digest]
            ]
            async with self._sessions() as session, session.begin():
                await scope_to_domain(session, domain_id)
                await store_vectors(session, domain_id, model=model.name, vectors=new)
            counts["embedded"] += len(new)
        return calls

    async def _call(self, model: EmbeddingModel, texts: list[str]) -> list[list[float]]:
        retry = self._settings.retry
        delay = retry.backoff_s
        for attempt in range(1, retry.attempts + 1):
            await self._pacer.wait()
            started = time.monotonic()
            try:
                vectors = await model.embed(texts)
            except PERMANENT_ERRORS:
                raise
            except InferenceError as error:
                if attempt >= retry.attempts:
                    raise
                log.warning(
                    "embedding call failed; retrying",
                    extra={"attempt": attempt, "error": f"{type(error).__name__}: {error}"},
                )
                await self._sleep(min(delay, retry.backoff_max_s))
                delay *= 2
                continue
            finally:
                EMBEDDING_CALL_DURATION.record(time.monotonic() - started)
            if len(vectors) != len(texts) or any(len(v) != self._dims for v in vectors):
                raise DimensionMismatchError(
                    f"embedding model {model.name!r} returned vectors that do not match "
                    f"{len(texts)} texts of {self._dims} dimensions"
                )
            return vectors
        raise AssertionError("unreachable")  # pragma: no cover


def _templates(schema: Schema | None, domain_id: str, branch: str) -> dict[str, KindTemplate]:
    """Resolved templates of the Kinds that have one; an unusable template is logged and
    its Kind left out (static validation refuses such schemas)."""
    found: dict[str, KindTemplate] = {}
    if schema is None:
        return found
    for kind in schema.kinds.values():
        try:
            template = resolve_template(kind)
        except TemplateError as error:
            log.warning(
                "embedding template skipped",
                extra={
                    "domain": domain_id,
                    "branch": branch,
                    "kind": kind.api_name,
                    "error": str(error),
                },
            )
            continue
        if template is not None:
            found[kind.id] = template
    return found
