"""The `extraction` job (design 9.2): documents in, a reviewable Proposal out.

1. Record the job in `extraction_jobs` against the schema of the Domain's default branch.
2. For each document: read the bytes from the blob store, parse, chunk, store the chunks and
   the text hash.
3. Find mentions: keys and titles (tier 0), spans of the local model (tier 1); resolve them
   against the default branch.
4. Find values (tier 0 rules), Links (co-occurrence and headings, tier 0; the local relation
   model, tier 1) and ask the chat model chunk by chunk (tier 2), within the token budget.
5. Assemble the items. With at least one item: create branch `extract/<job id>` from the
   default branch, open the Proposal (origin `extraction`), store the items, commit the items
   accepted at once on the branch (with `extraction` Lineage) and compute the merge preview.

Nothing is written to the default branch: reviewers decide item by item and merge the
Proposal. A tier 1 model that is not installed degrades to tier 0 with a warning in the job's
stats; a tier 2 model that cannot be created fails the job.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import lorebase_inference
from lorebase_core.model import Schema, new_ulid
from lorebase_inference import (
    InferenceSettings,
    ProviderUnavailableError,
    UnknownProviderError,
)
from lorebase_inference.interfaces import (
    ChatModel,
    Label,
    RelationExtractor,
    SpanExtractor,
    Usage,
)
from lorebase_scribe.extraction.assemble import CurrentState, assemble_items
from lorebase_scribe.extraction.candidates import (
    LinkCandidate,
    MatchCandidate,
    Mention,
    Resolved,
    ValueCandidate,
)
from lorebase_scribe.extraction.matching import (
    Dictionary,
    Resolver,
    TitleEntry,
    disambiguate,
    non_overlapping,
    tier0_mentions,
)
from lorebase_scribe.extraction.ontology import Ontology, build_ontology
from lorebase_scribe.extraction.parsing import ParseError, parse_document
from lorebase_scribe.extraction.relations import cooccurrence_links, heading_links, model_links
from lorebase_scribe.extraction.settings import ExtractionSettings
from lorebase_scribe.extraction.text import (
    Chunk,
    chunk_text,
    heading_sections,
    is_heading,
    sentence_bounds,
)
from lorebase_scribe.extraction.tier2 import run_tier2
from lorebase_scribe.extraction.values import heading_title, sentence_values
from lorebase_scribe.sources.sync import POLICY_KEY, domain_merge_policy
from lorebase_store.blobs import BlobNotFoundError, BlobStore, PostgresBlobStore
from lorebase_store.commit import ValidationFailedError
from lorebase_store.commit.schema import load_compiled
from lorebase_store.extraction import (
    EXTRACTION_BRANCH_PREFIX,
    MaterializeContext,
    Materialized,
    materialize_items,
    refresh_proposal,
)
from lorebase_store.materialize import create_branch
from lorebase_store.models.enums import ItemDecision, ProposalOrigin, ProposalStatus, RunStatus
from lorebase_store.reads import similar_titles
from lorebase_store.repositories import (
    ChunkRepository,
    ChunkRow,
    DocumentRepository,
    DocumentRow,
    DomainRepository,
    ExtractionCacheRepository,
    ExtractionJobRepository,
    ExtractionJobRow,
    ItemRow,
    MaterializedRepository,
    ProposalItemRepository,
    ProposalRepository,
    ProposalRow,
    scope_to_domain,
)
from lorebase_store.runtime import StoreRuntime

log = logging.getLogger("lorebase.scribe.extraction")

JOB_PREFIX = "exj"
PROPOSAL_PREFIX = "prp"
Clock = Callable[[], datetime]
_TIER1_ERRORS = (
    NotImplementedError,
    ProviderUnavailableError,
    UnknownProviderError,
    ImportError,
    OSError,
    ValueError,
)
DETERMINISTIC_RELATIONS = frozenset({"cooccurrence", "fake"})
"""Relation providers that pair spans of a sentence without a model, as tier 0 does."""


class ExtractionError(RuntimeError):
    """The job cannot succeed as queued (unknown document, unusable model configuration)."""


PERMANENT_ERRORS: tuple[type[Exception], ...] = (ExtractionError, ParseError, BlobNotFoundError)


def utc_now() -> datetime:
    return datetime.now(UTC)


# Models -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractionModels:
    """The models a job uses; `None` where a tier does not run or its model is missing."""

    spans: SpanExtractor | None = None
    span_model: str = ""
    relations: RelationExtractor | None = None
    relation_model: str = ""
    chat: ChatModel | None = None
    chat_model: str = ""
    warnings: tuple[str, ...] = ()

    def describe(self) -> str:
        parts = ["rules", self.span_model, self.relation_model, self.chat_model]
        return ",".join(p for p in parts if p)


def build_models(
    inference: InferenceSettings, settings: ExtractionSettings, tiers: Sequence[int]
) -> ExtractionModels:
    """Models of the configured providers for `tiers`."""
    warnings: list[str] = []
    spans: SpanExtractor | None = None
    span_model = relation_model = chat_model = ""
    relations: RelationExtractor | None = None
    chat: ChatModel | None = None
    local = inference.local_extractors
    if 1 in tiers:
        try:
            spans = lorebase_inference.create_span_extractor(inference)
            span_model = f"{local.provider}:{local.gliner.model}"
        except _TIER1_ERRORS as error:
            warnings.append(f"tier 1 span extractor unavailable: {error}")
        # A provider without a model pairs the spans of a sentence, which tier 0 already does.
        if 0 not in tiers or local.relation_provider not in DETERMINISTIC_RELATIONS:
            try:
                relations = lorebase_inference.create_relation_extractor(inference)
                relation_model = (
                    f"glirel:{local.glirel.model}"
                    if local.relation_provider == "glirel"
                    else local.relation_provider
                )
            except _TIER1_ERRORS as error:
                warnings.append(f"tier 1 relation extractor unavailable: {error}")
    if 2 in tiers:
        llm = inference.llm.for_task("extraction")
        try:
            chat = lorebase_inference.create_chat_model(inference, task="extraction")
        except (NotImplementedError, UnknownProviderError, ValueError) as error:
            raise ExtractionError(f"tier 2 chat model unavailable: {error}") from None
        chat_model = f"{llm.provider}:{llm.model}"
    return ExtractionModels(
        spans, span_model, relations, relation_model, chat, chat_model, tuple(warnings)
    )


# Store adapters ---------------------------------------------------------------------------------


class StoreLookup:
    """`RecordLookup` over the materialized head of a branch, one transaction per call."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        domain_id: str,
        branch: str,
        runtime: StoreRuntime,
    ) -> None:
        self._sessions = sessions
        self._domain = domain_id
        self._branch = branch
        self._runtime = runtime

    async def titles_of_keys(self, kind_id: str, keys: Sequence[str]) -> dict[str, str | None]:
        async with self._sessions() as session, session.begin():
            await scope_to_domain(session, self._domain)
            rows = await MaterializedRepository(
                session, self._domain, self._branch, writes=self._runtime.settings.writes
            ).records(kind_id, keys)
        return {key: row.title for key, row in rows.items()}

    async def similar(
        self, text: str, kind_ids: Sequence[str], *, threshold: float, limit: int
    ) -> list[MatchCandidate]:
        async with self._sessions() as session, session.begin():
            await scope_to_domain(session, self._domain)
            hits = await similar_titles(
                session,
                self._domain,
                self._branch,
                text,
                kind_ids=kind_ids,
                threshold=threshold,
                limit=limit,
                runtime=self._runtime,
            )
        return [MatchCandidate(h.kind_id, h.key, h.title, h.score, "trigram") for h in hits]


class StoreCache:
    """`AnswerCache` over `extraction_cache`."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, key: bytes) -> tuple[Any, Usage, str] | None:
        async with self._sessions() as session, session.begin():
            found = await ExtractionCacheRepository(session).get(key)
        if found is None:
            return None
        output, usage, model = found
        return (
            output,
            Usage(int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))),
            model,
        )

    async def put(self, key: bytes, data: Any, usage: Usage, model: str) -> None:
        async with self._sessions() as session, session.begin():
            await ExtractionCacheRepository(session).put(
                key,
                model=model,
                output=data,
                usage={"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens},
            )


# The run ----------------------------------------------------------------------------------------


@dataclass
class _Found:
    mentions: list[Resolved] = field(default_factory=list)
    values: list[ValueCandidate] = field(default_factory=list)
    links: list[LinkCandidate] = field(default_factory=list)


@dataclass
class ExtractionReport:
    job_id: str
    domain: str
    documents: list[str]
    branch: str
    target: str
    tiers: list[int]
    proposal_id: str | None = None
    items: int = 0
    accepted: int = 0
    pending: int = 0
    stats: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    warnings: list[str] = field(default_factory=list)
    materialized: dict[str, Any] | None = None
    duration_ms: int = 0

    def count(self, name: str, n: int = 1) -> None:
        self.stats[name] = int(self.stats.get(name, 0)) + n

    def as_dict(self) -> dict[str, Any]:
        return {
            "job": self.job_id,
            "domain": self.domain,
            "documents": list(self.documents),
            "branch": self.branch,
            "target": self.target,
            "tiers": list(self.tiers),
            "proposal": self.proposal_id,
            "items": self.items,
            "accepted": self.accepted,
            "pending": self.pending,
            "stats": dict(self.stats),
            "usage": dict(self.usage),
            "warnings": list(self.warnings),
            "materialized": self.materialized,
            "duration_ms": self.duration_ms,
        }


class ExtractionRun:
    """Runs extraction jobs against one database."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        runtime: StoreRuntime,
        settings: ExtractionSettings,
        inference: InferenceSettings,
        *,
        principal: str,
        models: Callable[[Sequence[int]], ExtractionModels] | None = None,
        blobs: BlobStore | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self._sessions = sessions
        self._runtime = runtime
        self._settings = settings
        self._inference = inference
        self._principal = principal
        self._models = models or (lambda tiers: build_models(inference, settings, tiers))
        self._blobs = blobs or PostgresBlobStore()
        self._clock = clock

    async def run(
        self,
        domain_id: str,
        document_ids: Sequence[str],
        *,
        tiers: Sequence[int] | None = None,
        requested_by: str | None = None,
    ) -> ExtractionReport:
        """Extract the documents into one Proposal on the Domain's default branch."""
        started = time.monotonic()
        chosen = sorted(set(tiers or self._settings.tiers))
        if not document_ids:
            raise ExtractionError("an extraction job needs at least one document")
        if not set(chosen) <= {0, 1, 2}:
            raise ExtractionError(f"unknown tiers {chosen}")
        models = self._models(chosen)
        job_id = f"{JOB_PREFIX}_{new_ulid()}"
        async with self._sessions() as session, session.begin():
            await scope_to_domain(session, domain_id)
            domain = await DomainRepository(session).get(domain_id)
            if domain is None:
                raise ExtractionError(f"no Domain {domain_id!r}")
            writes = self._runtime.settings.writes
            compiled = await MaterializedRepository(
                session, domain_id, domain.default_branch, writes=writes
            ).schema()
            if compiled is None:
                raise ExtractionError(f"branch {domain.default_branch!r} is not materialized")
            documents: list[DocumentRow] = []
            repository = DocumentRepository(session, domain_id)
            for document_id in dict.fromkeys(document_ids):
                row = await repository.get(document_id)
                if row is None:
                    raise ExtractionError(f"no document {document_id!r} in {domain_id!r}")
                documents.append(row)
            report = ExtractionReport(
                job_id=job_id,
                domain=domain_id,
                documents=[d.id for d in documents],
                branch=f"{EXTRACTION_BRANCH_PREFIX}{job_id}",
                target=domain.default_branch,
                tiers=chosen,
                warnings=list(models.warnings),
            )
            await ExtractionJobRepository(session, domain_id).insert(
                ExtractionJobRow(
                    id=job_id,
                    documents=report.documents,
                    schema_hash=compiled.schema_hash,
                    model=models.describe(),
                    prompt_version=self._settings.prompt_version,
                    status=RunStatus.RUNNING,
                    budget={"max_tokens": self._settings.max_tokens_per_job},
                    created_by=requested_by or self._principal,
                    branch=report.branch,
                    started_at=self._clock(),
                    stats={"tiers": chosen},
                )
            )
        schema = load_compiled(compiled.compiled)
        try:
            await self._run(domain, schema, compiled.schema_hash, documents, models, report)
        except Exception as error:
            report.duration_ms = int((time.monotonic() - started) * 1000)
            await self._finish(
                report, RunStatus.FAILED, {"type": type(error).__name__, "message": str(error)}
            )
            raise
        report.duration_ms = int((time.monotonic() - started) * 1000)
        await self._finish(report, RunStatus.SUCCEEDED)
        log.info(
            "documents extracted",
            extra={
                "domain": domain_id,
                "job": job_id,
                "proposal": report.proposal_id,
                "items": report.items,
                "accepted": report.accepted,
                "duration_ms": report.duration_ms,
            },
        )
        return report

    async def _finish(
        self, report: ExtractionReport, status: RunStatus, error: dict[str, Any] | None = None
    ) -> None:
        async with self._sessions() as session, session.begin():
            await scope_to_domain(session, report.domain)
            await ExtractionJobRepository(session, report.domain).update(
                report.job_id,
                status=status,
                finished_at=self._clock(),
                stats=report.as_dict(),
                usage=dict(report.usage),
                proposal_id=report.proposal_id,
                error=error,
            )

    # Extraction -------------------------------------------------------------------------

    async def _dictionary(self, domain_id: str, branch: str, ontology: Ontology) -> Dictionary:
        budget = self._settings.matching.dictionary_max_titles
        entries: list[TitleEntry] = []
        settings = self._runtime.settings
        async with self._sessions() as session, session.begin():
            await scope_to_domain(session, domain_id)
            rows = MaterializedRepository(session, domain_id, branch, writes=settings.writes)
            counts = await rows.record_counts()
            for kind_id, view in ontology.kinds.items():
                count = counts.get(kind_id, 0)
                if view.title_attribute is None or count == 0 or count > budget:
                    continue
                budget -= count
                async for page in rows.stream_records(
                    kind_id, batch=settings.materialize.read_batch_rows
                ):
                    entries.extend(TitleEntry(kind_id, r.key, r.title) for r in page if r.title)
        return Dictionary(entries, min_chars=self._settings.matching.min_title_chars)

    async def _run(
        self,
        domain: Any,
        schema: Schema,
        schema_hash: bytes,
        documents: Sequence[DocumentRow],
        models: ExtractionModels,
        report: ExtractionReport,
    ) -> None:
        ontology = build_ontology(schema, self._settings.new_record_kinds)
        dictionary = await self._dictionary(domain.id, report.target, ontology)
        resolver = Resolver(
            ontology,
            self._settings,
            StoreLookup(self._sessions, domain.id, report.target, self._runtime),
            dictionary,
        )
        found = _Found()
        for document in documents:
            await self._document(
                document, ontology, dictionary, resolver, schema_hash, models, report, found
            )
        current = await self._current(domain.id, report.target, found)
        assembled = assemble_items(
            ontology,
            found.mentions,
            found.values,
            found.links,
            current,
            job_id=report.job_id,
            principal=self._principal,
            now=self._clock(),
            settings=self._settings,
        )
        for name, count in assembled.stats.items():
            report.count(name, count)
        items = assembled.items
        report.items = len(items)
        if not items:
            return
        await self._propose(domain, schema, documents, items, report)

    async def _document(
        self,
        document: DocumentRow,
        ontology: Ontology,
        dictionary: Dictionary,
        resolver: Resolver,
        schema_hash: bytes,
        models: ExtractionModels,
        report: ExtractionReport,
        found: _Found,
    ) -> None:
        domain_id = report.domain
        async with self._sessions() as session, session.begin():
            await scope_to_domain(session, domain_id)
            data = await self._blobs.get(session, domain_id, document.blob_ref)
        parsed = await asyncio.to_thread(parse_document, data, document.media_type)
        text = parsed.text
        report.warnings.extend(f"{document.id}: {w}" for w in parsed.warnings)
        if len(text) > self._settings.max_document_chars:
            report.warnings.append(
                f"{document.id}: text cut at {self._settings.max_document_chars} characters"
            )
            text = text[: self._settings.max_document_chars]
        chunking = self._settings.chunking
        chunks = chunk_text(
            text, max_chars=chunking.max_chars, overlap_chars=chunking.overlap_chars
        )
        async with self._sessions() as session, session.begin():
            await scope_to_domain(session, domain_id)
            await ChunkRepository(session, domain_id).replace(
                document.id,
                [ChunkRow(c.seq, c.start, c.end, c.text, c.hash) for c in chunks],
            )
            await DocumentRepository(session, domain_id).set_text(
                document.id,
                text_hash=hashlib.sha256(text.encode("utf-8")).digest(),
                metadata={**parsed.metadata(), "chunks": len(chunks)},
            )
        report.count("chunks", len(chunks))
        sentences = sentence_bounds(text)
        tiers = report.tiers
        raw: list[Mention] = []
        for chunk in chunks:
            if 0 in tiers:
                raw.extend(
                    tier0_mentions(
                        ontology,
                        dictionary,
                        chunk.text,
                        offset=chunk.start,
                        chunk=chunk.seq,
                        document_id=document.id,
                        settings=self._settings,
                    )
                )
            if models.spans is not None:
                raw.extend(await self._spans(models, ontology, chunk, document.id))
        mentions = non_overlapping(raw)
        resolved = disambiguate([await resolver.resolve(m) for m in mentions], sentences)
        report.count("mentions", len(resolved))
        if 0 in tiers:
            found.values.extend(self._values(ontology, text, sentences, resolved))
            found.links.extend(
                cooccurrence_links(
                    ontology,
                    resolved,
                    text,
                    sentences,
                    document_id=document.id,
                    settings=self._settings,
                )
            )
            found.links.extend(
                heading_links(
                    ontology,
                    resolved,
                    text,
                    sentences,
                    document_id=document.id,
                    settings=self._settings,
                )
            )
        if models.relations is not None:
            for chunk in chunks:
                found.links.extend(
                    await model_links(
                        models.relations,
                        ontology,
                        resolved,
                        chunk.text,
                        chunk.start,
                        document_id=document.id,
                        model=models.relation_model,
                    )
                )
        found.mentions.extend(resolved)
        if models.chat is not None:
            await self._tier2(
                models, ontology, resolver, chunks, resolved, document, schema_hash, report, found
            )

    async def _spans(
        self, models: ExtractionModels, ontology: Ontology, chunk: Chunk, document_id: str
    ) -> list[Mention]:
        assert models.spans is not None
        labels = [
            Label(name=view.label, description=view.describe()) for view in ontology.kinds.values()
        ]
        mentions: list[Mention] = []
        for span in await models.spans.extract(chunk.text, labels):
            view = ontology.kind_by_label(span.label)
            if view is None:
                continue
            mentions.append(
                Mention(
                    kind_id=view.id,
                    text=span.text,
                    start=chunk.start + span.start,
                    end=chunk.start + span.end,
                    chunk=chunk.seq,
                    score=span.score,
                    tier=1,
                    source="span",
                    extractor=models.span_model,
                    document_id=document_id,
                )
            )
        return mentions

    def _values(
        self,
        ontology: Ontology,
        text: str,
        sentences: Sequence[tuple[int, int]],
        resolved: Sequence[Resolved],
    ) -> list[ValueCandidate]:
        """Values sentence by sentence: for the Records a sentence names, else for the Record
        its section heading names; titles of new Records from their headings."""
        excluded = [(r.mention.start, r.mention.end) for r in resolved]
        sections = heading_sections(text)
        found: list[ValueCandidate] = []
        for start, end in sentences:
            owners = [r for r in resolved if start <= r.mention.start < end]
            enclosing = sorted(
                (s for s in sections if s.start <= start < s.end),
                key=lambda s: s.start,
                reverse=True,
            )
            for section in enclosing:
                if owners:
                    break
                owners = [
                    r for r in resolved if section.start <= r.mention.start < section.heading_end
                ]
            if owners:
                found.extend(
                    sentence_values(
                        ontology,
                        owners,
                        text,
                        (start, end),
                        excluded=excluded,
                        settings=self._settings,
                    )
                )
        for item in resolved:
            if item.resolution.status == "new" and is_heading(text, item.mention.start):
                title = heading_title(ontology, item, text, settings=self._settings)
                if title is not None:
                    found.append(title)
        return found

    async def _tier2(
        self,
        models: ExtractionModels,
        ontology: Ontology,
        resolver: Resolver,
        chunks: Sequence[Chunk],
        resolved: Sequence[Resolved],
        document: DocumentRow,
        schema_hash: bytes,
        report: ExtractionReport,
        found: _Found,
    ) -> None:
        assert models.chat is not None
        budget = self._settings.max_tokens_per_job
        cache = StoreCache(self._sessions) if self._settings.cache else None
        for chunk in chunks:
            used = report.usage["input_tokens"] + report.usage["output_tokens"]
            if budget and used >= budget:
                report.count("chunks_over_budget")
                continue
            hints = [r for r in resolved if chunk.start <= r.mention.start < chunk.end]
            result = await run_tier2(
                models.chat,
                ontology,
                chunk,
                hints,
                resolver,
                document_id=document.id,
                schema_hash=schema_hash,
                model_key=models.chat_model,
                settings=self._settings,
                cache=cache,
            )
            report.count("model_calls", 0 if result.cached else 1)
            report.count("cache_hits", 1 if result.cached else 0)
            report.usage["input_tokens"] += result.usage.input_tokens
            report.usage["output_tokens"] += result.usage.output_tokens
            for reason, count in result.discarded.items():
                report.count(f"discarded_{reason}", count)
            found.mentions.extend(result.mentions)
            found.values.extend(result.values)
            found.links.extend(result.links)

    async def _current(self, domain_id: str, branch: str, found: _Found) -> CurrentState:
        keys: dict[str, set[str]] = defaultdict(set)
        for item in [
            *found.mentions,
            *(v.subject for v in found.values),
            *(side for link in found.links for side in (link.source, link.target)),
        ]:
            if item.usable and item.resolution.status != "new":
                keys[item.kind_id].add(str(item.key))
        pairs: dict[str, set[tuple[str, str]]] = defaultdict(set)
        for link in found.links:
            if link.source.usable and link.target.usable:
                pairs[link.relation_id].add((str(link.source.key), str(link.target.key)))
        records: dict[tuple[str, str], dict[str, Any]] = {}
        links: set[tuple[str, str, str]] = set()
        writes = self._runtime.settings.writes
        async with self._sessions() as session, session.begin():
            await scope_to_domain(session, domain_id)
            rows = MaterializedRepository(session, domain_id, branch, writes=writes)
            for kind_id, kind_keys in keys.items():
                for key, row in (await rows.records(kind_id, kind_keys)).items():
                    records[(kind_id, key)] = dict(row.values)
            for relation_id, relation_pairs in pairs.items():
                for pair in await rows.links(relation_id, relation_pairs):
                    links.add((relation_id, *pair))
        return CurrentState(records=records, links=frozenset(links))

    # Proposal ---------------------------------------------------------------------------

    async def _propose(
        self,
        domain: Any,
        schema: Schema,
        documents: Sequence[DocumentRow],
        items: list[ItemRow],
        report: ExtractionReport,
    ) -> None:
        policy = domain_merge_policy(domain)
        context = MaterializeContext(
            domain_id=domain.id,
            branch=report.branch,
            target=report.target,
            job_id=report.job_id,
            prompt_version=self._settings.prompt_version,
            actor=self._principal,
            runtime=self._runtime,
            clock=self._clock,
        )
        async with self._sessions() as session, session.begin():
            await scope_to_domain(session, domain.id)
            await create_branch(
                session,
                domain.id,
                report.branch,
                created_by=self._principal,
                source=report.target,
                runtime=self._runtime,
                clock=self._clock,
            )
            now = self._clock()
            proposal = ProposalRow(
                id=f"{PROPOSAL_PREFIX}_{new_ulid()}",
                title=f"Extract {report.job_id}",
                description=None,
                source_ref=report.branch,
                target_ref=report.target,
                origin=ProposalOrigin.EXTRACTION,
                status=ProposalStatus.OPEN,
                author=self._principal,
                created_at=now,
                checks={},
                policies={POLICY_KEY: policy.model_dump(mode="json", exclude_defaults=True)},
                idempotency_key=report.job_id,
                updated_at=now,
            )
            await ProposalRepository(session, domain.id).insert(proposal)
            repository = ProposalItemRepository(session, domain.id)
            await repository.insert(proposal.id, items)
            outcome = await self._materialize(session, context, schema, proposal, items, report)
            stored = await repository.all(proposal.id)
            await refresh_proposal(
                session,
                context,
                proposal,
                stored,
                schema,
                outcome,
                documents=[d.filename for d in documents],
                policy=policy,
            )
            await ExtractionJobRepository(session, domain.id).update(
                report.job_id, proposal_id=proposal.id
            )
        report.proposal_id = proposal.id
        report.accepted = sum(1 for i in stored if i.decision is ItemDecision.ACCEPTED)
        report.pending = sum(1 for i in stored if i.decision is ItemDecision.PENDING)
        report.materialized = outcome.as_dict()

    async def _materialize(
        self,
        session: AsyncSession,
        context: MaterializeContext,
        schema: Schema,
        proposal: ProposalRow,
        items: list[ItemRow],
        report: ExtractionReport,
    ) -> Materialized:
        """Commit the items accepted at once; when the branch rejects them, leave them pending
        for review instead."""
        if not any(i.decision is ItemDecision.ACCEPTED for i in items):
            return Materialized()
        message = f"Extraction {context.job_id}: items accepted at once"
        try:
            async with session.begin_nested():
                return await materialize_items(session, context, items, schema, message=message)
        except ValidationFailedError as error:
            report.warnings.append(f"items accepted at once were left pending: {error}")
            repository = ProposalItemRepository(session, context.domain_id)
            for item in items:
                if item.decision is ItemDecision.ACCEPTED:
                    await repository.decide(
                        proposal.id,
                        item.item_id,
                        decision=ItemDecision.PENDING,
                        decided_by=None,
                        decided_at=None,
                        edits=None,
                    )
            return Materialized()
