"""One synchronization run of a structured Source (design 9.1).

1. Resolve the Source in the schema of the Domain's default branch (the target branch) and
   record the run in `source_runs`.
2. Read the rows (incremental Sources from the watermark of the last successful run) and map
   them; rows that do not fit are skipped and reported, up to `max_row_errors`.
3. Prepare the Source branch (`target_branch_policy.branch_template`, default
   `source/{source}`): create it from the target branch, or, when no Proposal from it is open
   and its head differs from the target's, recreate it at the target head, so every run
   compares the Source with what the target holds now.
4. Compute the delta against the branch head and commit it in batches of `commit_batch`
   changes with origin `source_sync` and one provenance entry per changed row (Source id, run
   id, row locator).
5. Open the Source's Proposal into the target branch, or update the open one (title with the
   run's counts, idempotent per run), then apply the merge policy: `proposal` computes the
   merge preview only; `auto_when_clean` merges when no Conflict remains; and
   `auto_with_authority` also resolves value Conflicts on Attributes whose `authority` is the
   Source in the Source's favor.

A dry run reads, maps and computes the delta against the state step 3 would produce, then
records the counts without writing to any branch.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from pydantic import JsonValue, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.merge import MergePolicy, Side
from lorebase_core.model import (
    IdPrefix,
    ProvenanceEntry,
    Schema,
    Source,
    SourceSyncOrigin,
    new_id,
    new_ulid,
)
from lorebase_core.model.source import MergePolicy as SourcePolicy
from lorebase_core.model.source import SyncMode
from lorebase_core.revision import Author, Origin
from lorebase_core.types.encoding import format_timestamp
from lorebase_scribe.settings import SourceSettings
from lorebase_scribe.sources.connectors import ConnectorFactory, default_connector
from lorebase_scribe.sources.delta import (
    CurrentState,
    Delta,
    StoredRecord,
    batches,
    compute_delta,
)
from lorebase_scribe.sources.mapping import MappedRow, RowError, RowMapper, SourceDefinitionError
from lorebase_scribe.telemetry import SOURCE_CHANGES, SOURCE_ROWS, tracer
from lorebase_store.commit import Change, ValidationFailedError, commit
from lorebase_store.commit.schema import load_compiled
from lorebase_store.materialize import create_branch
from lorebase_store.merging import AttemptResult, MergeRequest, MergeService
from lorebase_store.models.enums import ProposalOrigin, ProposalStatus, RunStatus
from lorebase_store.repositories import (
    DomainRepository,
    DomainRow,
    MaterializedRepository,
    ProposalFilter,
    ProposalRepository,
    ProposalRow,
    RefRepository,
    SourceRunRepository,
    SourceRunRow,
    scope_to_domain,
)
from lorebase_store.runtime import StoreRuntime

log = logging.getLogger("lorebase.scribe.sources")

RUN_PREFIX = "srn"
PROPOSAL_PREFIX = "prp"
POLICY_KEY = "merge_policy"
"""Key of the merge policy in a Domain's settings and in a Proposal's `policies`."""
SYNC_ORIGIN = "source_sync"
ISSUE_SAMPLES = 10

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class SourceNotFoundError(LookupError):
    """The Domain or the Source does not exist."""


class SourceSyncError(RuntimeError):
    """The run cannot succeed as is (bad rows, rejected changes); retrying will not help."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


PERMANENT_ERRORS: tuple[type[Exception], ...] = (
    SourceNotFoundError,
    SourceDefinitionError,
    SourceSyncError,
)
"""Failures a retry of the same job cannot fix."""


# Watermarks -----------------------------------------------------------------------------------


_LEGACY_BYTES = re.compile(r"b'.*'|b\".*\"", re.S)


def encode_watermark(value: Any) -> dict[str, JsonValue]:
    """A watermark value as JSON with its type, so the next run binds the same native type."""
    if isinstance(value, bytes | bytearray | memoryview):
        # SQL Server rowversion is binary(8): hex keeps it JSON-safe and bound back as bytes.
        return {"type": "bytes", "value": bytes(value).hex()}
    if isinstance(value, datetime):
        return {"type": "timestamp", "value": value.isoformat()}
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, bool):
        return {"type": "text", "value": str(value).lower()}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, Decimal):
        return {"type": "decimal", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": repr(value)}
    return {"type": "text", "value": str(value)}


def decode_watermark(stored: Any) -> Any:
    if not isinstance(stored, dict) or "value" not in stored:
        return None
    kind, text = stored.get("type"), str(stored["value"])
    if kind == "bytes":
        return bytes.fromhex(text)
    if kind == "text" and _LEGACY_BYTES.fullmatch(text):
        return None  # the repr of bytes stored by an older run: it cannot be bound, resync
    if kind == "timestamp":
        return datetime.fromisoformat(text)
    if kind == "date":
        return date.fromisoformat(text)
    if kind == "integer":
        return int(text)
    if kind == "decimal":
        return Decimal(text)
    if kind == "float":
        return float(text)
    return text


def _later(current: Any, candidate: Any) -> Any:
    candidate = bytes(candidate) if isinstance(candidate, bytearray | memoryview) else candidate
    if current is None:
        return candidate
    try:
        return candidate if candidate > current else current
    except TypeError:
        return candidate if str(candidate) > str(current) else current


# Report ---------------------------------------------------------------------------------------


@dataclass
class SyncReport:
    """Outcome of a run, stored as the run's stats and returned as the job result."""

    run_id: str
    domain: str
    source: str
    source_name: str
    branch: str
    target: str
    dry_run: bool
    rows_read: int = 0
    rows_mapped: int = 0
    rows_failed: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    revisions: list[str] = field(default_factory=list)
    proposal_id: str | None = None
    proposal_status: str | None = None
    merge_status: str | None = None
    conflicts: int = 0
    errors: list[dict[str, JsonValue]] = field(default_factory=list)
    watermark: dict[str, JsonValue] | None = None
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "run": self.run_id,
            "domain": self.domain,
            "source": self.source,
            "source_name": self.source_name,
            "branch": self.branch,
            "target": self.target,
            "dry_run": self.dry_run,
            "rows": {
                "read": self.rows_read,
                "mapped": self.rows_mapped,
                "failed": self.rows_failed,
            },
            "counts": dict(self.counts),
            "revisions": list(self.revisions),
            "proposal": self.proposal_id,
            "proposal_status": self.proposal_status,
            "merge_status": self.merge_status,
            "conflicts": self.conflicts,
            "errors": list(self.errors),
            "watermark": self.watermark,
            "duration_ms": self.duration_ms,
        }


@dataclass
class _Read:
    rows: list[MappedRow] = field(default_factory=list)
    watermark: Any = None


# Helpers --------------------------------------------------------------------------------------


def find_source(schema: Schema, reference: str) -> Source | None:
    """A Source by id or api name."""
    return schema.sources.get(reference) or schema.source_by_api_name(reference)


def source_branch(source: Source) -> str:
    template = source.target_branch_policy.branch_template
    try:
        name = template.format(source=source.api_name)
    except (KeyError, IndexError, ValueError):
        raise SourceDefinitionError(f"invalid branch template {template!r}") from None
    if not name.strip():
        raise SourceDefinitionError(f"branch template {template!r} gives an empty name")
    return name


def domain_merge_policy(domain: DomainRow) -> MergePolicy:
    """The Domain's merge policy setting; the default policy when it is missing or invalid."""
    raw = (domain.settings or {}).get(POLICY_KEY) or {}
    try:
        return MergePolicy.model_validate(raw)
    except ValidationError:
        log.warning("invalid merge policy setting", extra={"domain": domain.id})
        return MergePolicy()


def merge_policy(domain: DomainRow, schema: Schema, source: Source) -> MergePolicy:
    """The policy of the Source's merges: the Domain's, plus, for `auto_with_authority`, the
    Source's side on every Attribute it is the authority of."""
    policy = domain_merge_policy(domain)
    if source.target_branch_policy.merge_policy is not SourcePolicy.AUTO_WITH_AUTHORITY:
        return policy
    kind = schema.kinds.get(source.target_kind)
    authority = {
        attribute.id: Side.THEIRS
        for attribute in (kind.attributes.values() if kind else ())
        if attribute.authority == source.id
    }
    return policy.model_copy(
        update={"prefer_attributes": {**policy.prefer_attributes, **authority}}
    )


def _issues(error: ValidationFailedError) -> list[dict[str, Any]]:
    return [
        {"code": issue.code.value, "message": issue.message, "key": issue.key}
        for issue in error.issues[:ISSUE_SAMPLES]
    ]


# The run ----------------------------------------------------------------------------------------


class SourceSync:
    """Runs Source synchronizations against one database."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        runtime: StoreRuntime,
        settings: SourceSettings,
        *,
        principal: str,
        connectors: ConnectorFactory = default_connector,
        clock: Clock = utc_now,
    ) -> None:
        self._sessions = sessions
        self._runtime = runtime
        self._settings = settings
        self._principal = principal
        self._connectors = connectors
        self._clock = clock

    async def run(self, domain_id: str, reference: str, *, dry_run: bool = False) -> SyncReport:
        """Synchronize the Source `reference` (id or api name) of a Domain."""
        started = time.monotonic()
        async with self._sessions() as session, session.begin():
            await scope_to_domain(session, domain_id)
            domain = await DomainRepository(session).get(domain_id)
            if domain is None:
                raise SourceNotFoundError(f"no Domain {domain_id!r}")
            schema = await self._schema(session, domain_id, domain.default_branch)
            source = find_source(schema, reference)
            if source is None:
                raise SourceNotFoundError(f"no Source {reference!r} in Domain {domain_id!r}")
            report = SyncReport(
                run_id=f"{RUN_PREFIX}_{new_ulid()}",
                domain=domain_id,
                source=source.id,
                source_name=source.api_name,
                branch=source_branch(source),
                target=domain.default_branch,
                dry_run=dry_run,
            )
            await SourceRunRepository(session, domain_id).insert(
                SourceRunRow(
                    id=report.run_id,
                    source_id=source.id,
                    status=RunStatus.RUNNING,
                    started_at=self._clock(),
                    finished_at=None,
                    stats={"dry_run": dry_run},
                )
            )
        with tracer.start_as_current_span(
            "scribe.source_sync",
            attributes={
                "lorebase.domain": domain_id,
                "lorebase.source": source.api_name,
                "lorebase.run": report.run_id,
                "lorebase.dry_run": dry_run,
            },
        ) as span:
            try:
                await self._run(domain, schema, source, report)
            except Exception as error:
                report.duration_ms = int((time.monotonic() - started) * 1000)
                details = error.details if isinstance(error, SourceSyncError) else {}
                await self._finish(
                    report,
                    RunStatus.FAILED,
                    error={"type": type(error).__name__, "message": str(error), **details},
                )
                span.record_exception(error)
                raise
            report.duration_ms = int((time.monotonic() - started) * 1000)
            await self._finish(report, RunStatus.SUCCEEDED)
            span.set_attribute("lorebase.changes", sum(report.counts.values()))
        log.info(
            "source synchronized",
            extra={
                "domain": domain_id,
                "source": source.api_name,
                "run": report.run_id,
                "dry_run": dry_run,
                "rows": report.rows_read,
                **report.counts,
                "proposal": report.proposal_id,
                "merge_status": report.merge_status,
                "duration_ms": report.duration_ms,
            },
        )
        return report

    async def _schema(self, session: AsyncSession, domain_id: str, branch: str) -> Schema:
        writes = self._runtime.settings.writes
        compiled = await MaterializedRepository(session, domain_id, branch, writes=writes).schema()
        if compiled is None:
            raise SourceNotFoundError(f"branch {branch!r} of {domain_id!r} is not materialized")
        return load_compiled(compiled.compiled)

    async def _finish(
        self, report: SyncReport, status: RunStatus, error: dict[str, Any] | None = None
    ) -> None:
        stats = report.as_dict()
        revision = bytes.fromhex(report.revisions[-1]) if report.revisions else None
        async with self._sessions() as session, session.begin():
            await scope_to_domain(session, report.domain)
            await SourceRunRepository(session, report.domain).finish(
                report.run_id,
                status=status,
                finished_at=self._clock(),
                stats=stats,
                revision_id=revision,
                proposal_id=report.proposal_id,
                error=error,
            )

    async def _run(
        self, domain: DomainRow, schema: Schema, source: Source, report: SyncReport
    ) -> None:
        mapper = RowMapper(schema, source)
        since = await self._since(domain.id, source)
        read = await self._read(mapper, source, since, report)
        async with self._sessions() as session, session.begin():
            await scope_to_domain(session, domain.id)
            state_branch = await self._prepare_branch(session, domain, report)
            current = await self._current(session, domain.id, state_branch, mapper, source)
        delete_missing = source.sync_mode is SyncMode.SNAPSHOT and report.rows_failed == 0
        delta = compute_delta(
            mapper,
            read.rows,
            current,
            delete_missing=delete_missing,
            provenance=lambda row: self._provenance(source, report.run_id, row),
        )
        report.counts = delta.counts()
        SOURCE_CHANGES.add(
            delta.change_count, {"lorebase.source": source.api_name, "dry_run": report.dry_run}
        )
        if report.dry_run:
            return
        await self._commit(domain.id, source, delta, report)
        if read.watermark is not None:
            report.watermark = encode_watermark(read.watermark)
        else:
            previous = await self._previous_watermark(domain.id, source)
            report.watermark = previous
        await self._propose(domain, schema, source, report)

    # Reading --------------------------------------------------------------------------------

    async def _previous_watermark(
        self, domain_id: str, source: Source
    ) -> dict[str, JsonValue] | None:
        if source.sync_mode is not SyncMode.INCREMENTAL:
            return None
        async with self._sessions() as session, session.begin():
            await scope_to_domain(session, domain_id)
            runs = await SourceRunRepository(session, domain_id).page(source.id, limit=50)
        for run in runs:
            stats = run.stats or {}
            if run.status is RunStatus.SUCCEEDED and not stats.get("dry_run"):
                watermark = stats.get("watermark")
                return watermark if isinstance(watermark, dict) else None
        return None

    async def _since(self, domain_id: str, source: Source) -> Any:
        return decode_watermark(await self._previous_watermark(domain_id, source))

    async def _read(
        self, mapper: RowMapper, source: Source, since: Any, report: SyncReport
    ) -> _Read:
        connector = self._connectors(source, self._settings)
        read = _Read()
        limit = self._settings.max_row_errors
        watermark_column = source.watermark_column
        checked = False
        # Keys seen in the rows of a Source without fixed columns (JSON records): the mapped
        # columns are checked against all of them once the rows are read.
        seen: dict[str, None] = {}
        async for locator, row in connector.rows(since):
            if not connector.columns:
                seen.update(dict.fromkeys(row))
            elif not checked:
                mapper.check_columns(connector.columns)
                checked = True
            report.rows_read += 1
            if watermark_column is not None and row.get(watermark_column) not in (None, ""):
                read.watermark = _later(read.watermark, row[watermark_column])
            try:
                read.rows.append(mapper.map(row, locator))
            except RowError as error:
                report.rows_failed += 1
                if len(report.errors) < self._settings.error_samples:
                    report.errors.append(error.as_dict())
                if report.rows_failed > limit:
                    if seen:  # a mapped column no row had explains the failures better
                        mapper.check_columns(tuple(seen))
                    raise SourceSyncError(
                        f"more than {limit} rows failed mapping; nothing was committed",
                        {"rows_failed": report.rows_failed},
                    ) from None
        if seen:
            mapper.check_columns(tuple(seen))
        report.rows_mapped = len(read.rows)
        SOURCE_ROWS.add(report.rows_read, {"lorebase.source": source.api_name})
        return read

    # Branch and state ---------------------------------------------------------------------------

    async def _open_proposal(
        self, session: AsyncSession, domain_id: str, branch: str, target: str
    ) -> ProposalRow | None:
        found = await ProposalRepository(session, domain_id).page(
            ProposalFilter(status=ProposalStatus.OPEN, source_ref=branch, target_ref=target),
            limit=1,
        )
        return found[0] if found else None

    async def _prepare_branch(
        self, session: AsyncSession, domain: DomainRow, report: SyncReport
    ) -> str:
        """Create or refresh the Source branch; returns the branch the delta is computed on."""
        refs = RefRepository(session, domain.id)
        branch, target = report.branch, report.target
        ref = await refs.get(branch)
        target_ref = await refs.require(target)
        stale = (
            ref is not None
            and ref.head != target_ref.head
            and not ref.protected
            and await self._open_proposal(session, domain.id, branch, target) is None
        )
        if report.dry_run:
            return target if ref is None or stale else branch
        if ref is not None and not stale:
            return branch
        if ref is not None:
            await refs.lock(branch, timeout_ms=self._runtime.settings.commit.lock_timeout_ms)
            await refs.delete(branch)
        await create_branch(
            session,
            domain.id,
            branch,
            created_by=self._principal,
            source=target,
            runtime=self._runtime,
            clock=self._clock,
        )
        return branch

    async def _current(
        self,
        session: AsyncSession,
        domain_id: str,
        branch: str,
        mapper: RowMapper,
        source: Source,
    ) -> CurrentState:
        settings = self._runtime.settings
        rows = MaterializedRepository(session, domain_id, branch, writes=settings.writes)
        batch = settings.materialize.read_batch_rows
        records: dict[str, StoredRecord] = {}
        async for page in rows.stream_records(mapper.kind.id, batch=batch):
            for row in page:
                records[row.key] = StoredRecord(row.object_hash, row.values)
        owned = await rows.record_keys_with_origin(
            mapper.kind.id,
            origin_type=SYNC_ORIGIN,
            origin_field="source_id",
            origin_value=source.id,
        )
        links: dict[str, dict[str, frozenset[str]]] = {}
        for relation_id in mapper.relations:
            by_from: dict[str, set[str]] = defaultdict(set)
            async for link_page in rows.stream_links(relation_id, batch=batch):
                for link in link_page:
                    by_from[link.from_key].add(link.to_key)
            links[relation_id] = {key: frozenset(to) for key, to in by_from.items()}
        return CurrentState(records=records, owned=frozenset(owned), links=links)

    def _provenance(self, source: Source, run_id: str, row: MappedRow) -> ProvenanceEntry:
        return ProvenanceEntry(
            id=new_id(IdPrefix.PROVENANCE),
            origin=SourceSyncOrigin(source_id=source.id, run_id=run_id, row_locator=row.locator),
            actor=self._principal,
            at=format_timestamp(self._clock()),
        )

    # Writing ----------------------------------------------------------------------------------

    async def _commit(
        self, domain_id: str, source: Source, delta: Delta, report: SyncReport
    ) -> None:
        planned = batches(delta.groups, self._settings.commit_batch)
        counts = delta.counts()
        summary = f"+{counts['added']} ~{counts['changed']} -{counts['removed']}"
        for index, batch in enumerate(planned, start=1):
            changes = [change for group in batch for change in group.changes]
            entries = {c.provenance.id: c.provenance for c in changes if c.provenance is not None}
            part = f" (batch {index} of {len(planned)})" if len(planned) > 1 else ""
            try:
                async with self._sessions() as session, session.begin():
                    result = await commit(
                        session,
                        domain_id,
                        report.branch,
                        [
                            Change(c.change, None if c.provenance is None else c.provenance.id)
                            for c in changes
                        ],
                        author=Author(principal=self._principal),
                        message=f"Sync {source.api_name}: {summary}{part}",
                        origin=Origin(type=SYNC_ORIGIN, ref=report.run_id),
                        provenance=list(entries.values()),
                        runtime=self._runtime,
                        clock=self._clock,
                    )
            except ValidationFailedError as error:
                raise SourceSyncError(
                    f"the branch rejected batch {index}: {error}",
                    {"issues": _issues(error)},
                ) from None
            if not result.no_op:
                report.revisions.append(result.revision_id.hex())

    async def _propose(
        self, domain: DomainRow, schema: Schema, source: Source, report: SyncReport
    ) -> None:
        policy = merge_policy(domain, schema, source)
        mode = source.target_branch_policy.merge_policy
        counts = report.counts
        title = (
            f"Sync {source.api_name}: {counts.get('added', 0)} added, "
            f"{counts.get('changed', 0)} changed, {counts.get('removed', 0)} removed"
        )
        description = (
            f"Source {source.api_name} ({source.id}), run {report.run_id}: "
            f"{report.rows_read} rows read, {report.rows_failed} skipped; "
            f"links +{counts.get('links_added', 0)} -{counts.get('links_removed', 0)}."
        )
        async with self._sessions() as session, session.begin():
            await scope_to_domain(session, domain.id)
            proposals = ProposalRepository(session, domain.id)
            proposal = await proposals.by_key(self._principal, report.run_id)
            if proposal is None:
                proposal = await self._open_proposal(
                    session, domain.id, report.branch, report.target
                )
            automatic = mode is not SourcePolicy.PROPOSAL
            if not report.revisions and (proposal is None or not automatic):
                # Nothing new: an open Proposal is left as it is (a no-op run).
                if proposal is not None:
                    report.proposal_id = proposal.id
                    report.proposal_status = proposal.status.value
                return
            now = self._clock()
            stored = {POLICY_KEY: policy.model_dump(mode="json", exclude_defaults=True)}
            if proposal is None:
                proposal = ProposalRow(
                    id=f"{PROPOSAL_PREFIX}_{new_ulid()}",
                    title=title,
                    description=description,
                    source_ref=report.branch,
                    target_ref=report.target,
                    origin=ProposalOrigin.SOURCE_SYNC,
                    status=ProposalStatus.OPEN,
                    author=self._principal,
                    created_at=now,
                    checks={},
                    policies=stored,
                    idempotency_key=report.run_id,
                    updated_at=now,
                )
                await proposals.insert(proposal)
            elif report.revisions and proposal.status is ProposalStatus.OPEN:
                updated = await proposals.update(
                    proposal.id, at=now, title=title, description=description, policies=stored
                )
                assert updated is not None
                proposal = updated
            report.proposal_id = proposal.id
            if proposal.status is not ProposalStatus.OPEN:
                report.proposal_status = proposal.status.value
                return
            result = await self._merge(session, domain.id, proposal, policy, mode, report)
            report.merge_status = result.attempt.status.value
            if result.computation is not None:
                report.conflicts = result.computation.open_count
            final = await proposals.get(proposal.id)
            report.proposal_status = None if final is None else final.status.value

    async def _merge(
        self,
        session: AsyncSession,
        domain_id: str,
        proposal: ProposalRow,
        policy: MergePolicy,
        mode: SourcePolicy,
        report: SyncReport,
    ) -> AttemptResult:
        service = MergeService(session, domain_id, runtime=self._runtime, clock=self._clock)
        request = MergeRequest(
            proposal=proposal,
            actor=self._principal,
            policy=policy,
            idempotency_key=report.run_id,
        )
        automatic = mode in (SourcePolicy.AUTO_WHEN_CLEAN, SourcePolicy.AUTO_WITH_AUTHORITY)
        if automatic and not self._settings.auto_merge_protected:
            target = await RefRepository(session, domain_id).require(report.target)
            automatic = not target.protected
        if automatic:
            return await service.merge(request)
        return await service.recompute(request)
