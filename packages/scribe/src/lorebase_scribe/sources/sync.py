"""One synchronization run of a structured Source (design 9.1).

1. Resolve the Source in the schema of the Domain's default branch (the target branch) and
   record the run in `source_runs`.
2. Read the rows (incremental Sources from the watermark of the last successful run) and map
   them; rows that do not fit are skipped and reported, up to `max_row_errors`.
3. Prepare the Source branch (`target_branch_policy.branch_template`, default
   `source/{source}`): create it from the target branch, or, when no Proposal from it is open
   and its head differs from the target's, recreate it at the target head, so every run
   compares the Source with what the target holds now.
4. Read the Source's last-written state (`source_written`, the open Proposal's pending state
   over it) and who set the branch's values, compute the delta (`delta`: upstream changes
   only; local edits per `local_edits`, design 9.1 "Corrections that stick") and commit it in
   batches of `commit_batch` changes with origin `source_sync` and one provenance entry per
   changed row (Source id, run id, row locator).
5. Open the Source's Proposal into the target branch, or update the open one (title with the
   run's counts, idempotent per run), and stage the run's last-written state in it (it
   becomes the state when the Proposal merges, `MergeService`; a run with nothing to merge
   records it at once). Raise the reconciliation candidates of the run's changes against the
   target head (design 10.6, `lorebase_scribe.reconcile.sources`; linked to the Proposal, so
   its `reconciliation` check sees them). Under `local_edits: keep`, resolve the local-edit
   Conflicts of the merge attempt by the ranking of `delta.KEPT_ORIGINS` (resolver
   `policy:local_edits`). Then apply the merge policy: `proposal` computes the merge preview
   only; `auto_when_clean` merges when no Conflict remains, local-edit Conflicts included;
   and `auto_with_authority` also writes the Attributes whose `authority` is the Source over
   local edits and resolves their value Conflicts in the Source's favor. Every local edit
   is audited as `source.local_edit_conflict`.
6. Incremental Sources: record that the run read each mapped Record upstream
   (`source_written.last_seen_run`), so the reconciliation sweep can raise `source_unseen`
   candidates for Records not seen within the Source's `stale_after` (design 9.1).

A dry run reads, maps and computes the delta against the state step 3 would produce, then
records the counts (local edits and seeding included) without writing to any branch.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from pydantic import JsonValue, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.merge import MergePolicy, Resolution, ResolutionChoice, Side
from lorebase_core.model import (
    IdPrefix,
    LocalEdits,
    ProvenanceEntry,
    Schema,
    Source,
    SourceSyncOrigin,
    new_id,
    new_ulid,
)
from lorebase_core.model.source import MergePolicy as SourcePolicy
from lorebase_core.model.source import SyncMode
from lorebase_core.reconcile import ReconcileOptions
from lorebase_core.revision import Author, Origin
from lorebase_core.tree import kind_partition
from lorebase_core.types.encoding import format_timestamp
from lorebase_scribe.reconcile.detector import Detector, Raised, SemanticStage, effective_options
from lorebase_scribe.reconcile.sources import sync_disagreements
from lorebase_scribe.settings import SourceSettings
from lorebase_scribe.sources.connectors import ConnectorFactory, default_connector
from lorebase_scribe.sources.delta import (
    CurrentState,
    Delta,
    EditRules,
    Outcome,
    Setter,
    StoredRecord,
    WrittenState,
    batches,
    compute_delta,
)
from lorebase_scribe.sources.mapping import (
    MappedRow,
    Row,
    RowError,
    RowMapper,
    SourceDefinitionError,
)
from lorebase_scribe.telemetry import SOURCE_CHANGES, SOURCE_ROWS, tracer
from lorebase_store.approval_rules import (
    HELD_BY_APPROVALS,
    ApprovalRuleSettings,
    effective_rules,
)
from lorebase_store.auth import AuditRepository, NewAuditEntry
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
    ResolutionRepository,
    ResolutionRow,
    SourceRunRepository,
    SourceRunRow,
    SourceWrittenRepository,
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
LOCAL_EDITS_RESOLVER = "policy:local_edits"
"""Resolver of the local-edit Conflicts a `keep` Source resolves (design 6.10)."""
LOCAL_EDIT_ACTION = "source.local_edit_conflict"
"""Audit action of each local edit a run meets (design 9.1)."""

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
    #: Why an automatic merge was not performed: `approvals` (the protected target's steward
    #: or role rules, named in `blocking_rules`, require a person).
    merge_blocked: str | None = None
    blocking_rules: list[str] = field(default_factory=list)
    conflicts: int = 0
    errors: list[dict[str, JsonValue]] = field(default_factory=list)
    watermark: dict[str, JsonValue] | None = None
    duration_ms: int = 0
    #: Local edits met, by outcome (`conflict`, `kept`, `overwritten`, `deleted`).
    local_edits: dict[str, int] = field(default_factory=dict)
    #: Keys seen without last-written state.
    seeded: int = 0

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
            "merge_blocked": self.merge_blocked,
            "blocking_rules": list(self.blocking_rules),
            "conflicts": self.conflicts,
            "errors": list(self.errors),
            "watermark": self.watermark,
            "duration_ms": self.duration_ms,
            "local_edits": dict(self.local_edits),
            "seeded": self.seeded,
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


def authority_attributes(schema: Schema, source: Source) -> frozenset[str]:
    """Attributes whose `authority` is the Source, under `auto_with_authority` (else none)."""
    if source.target_branch_policy.merge_policy is not SourcePolicy.AUTO_WITH_AUTHORITY:
        return frozenset()
    kind = schema.kinds.get(source.target_kind)
    return frozenset(
        attribute.id
        for attribute in (kind.attributes.values() if kind else ())
        if attribute.authority == source.id
    )


def merge_policy(domain: DomainRow, schema: Schema, source: Source) -> MergePolicy:
    """The policy of the Source's merges: the Domain's, plus, for `auto_with_authority`, the
    Source's side on every Attribute it is the authority of."""
    policy = domain_merge_policy(domain)
    authority = dict.fromkeys(authority_attributes(schema, source), Side.THEIRS)
    if not authority:
        return policy
    return policy.model_copy(
        update={"prefer_attributes": {**policy.prefer_attributes, **authority}}
    )


def _source_origin(source: Source) -> dict[str, str]:
    """The Lineage filter of the values a Source wrote."""
    return {"origin_type": SYNC_ORIGIN, "origin_field": "source_id", "origin_value": source.id}


async def _setters(
    rows: MaterializedRepository, kind_id: str, source: Source
) -> dict[tuple[str, str], Setter]:
    """Who set each value of `kind_id` that `source` did not write, by key and Attribute."""
    return {
        (found.key, found.attribute): Setter(found.origin_type, found.actor)
        for found in await rows.attribute_origins_except(kind_id, **_source_origin(source))
    }


def edit_rules(schema: Schema, source: Source) -> EditRules:
    """How the Source's runs treat local edits (design 9.1)."""
    return EditRules(source.local_edit_mode, authority_attributes(schema, source))


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
        reconciliation: ReconcileOptions | None = None,
        semantic: SemanticStage | None = None,
        approval_rules: ApprovalRuleSettings | None = None,
    ) -> None:
        self._sessions = sessions
        self._reconciliation = reconciliation
        self._semantic = semantic
        self._approval_rules = approval_rules or ApprovalRuleSettings()
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
        mapper = RowMapper(schema, source, limits=self._runtime.schema.expression_limits)
        since = await self._since(domain.id, source)
        read = await self._read(mapper, source, since, report)
        async with self._sessions() as session, session.begin():
            await scope_to_domain(session, domain.id)
            state_branch = await self._prepare_branch(session, domain, report)
            current = await self._current(session, domain.id, state_branch, mapper, source)
            written = await self._written(session, domain.id, mapper, source, report)
        delete_missing = source.sync_mode is SyncMode.SNAPSHOT and report.rows_failed == 0
        delta = compute_delta(
            mapper,
            read.rows,
            current,
            written,
            delete_missing=delete_missing,
            provenance=lambda row: self._provenance(source, report.run_id, row),
            rules=edit_rules(schema, source),
        )
        report.counts = delta.counts()
        report.local_edits = delta.local_edit_counts()
        report.seeded = delta.seeded
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
        await self._propose(domain, schema, source, report, (mapper, delta))
        if source.sync_mode is SyncMode.INCREMENTAL:
            await self._seen(domain.id, source, report, mapper, read.rows)

    async def _seen(
        self,
        domain_id: str,
        source: Source,
        report: SyncReport,
        mapper: RowMapper,
        rows: Sequence[MappedRow],
    ) -> None:
        """Record that the run read these Records upstream (`last_seen_run`, design 9.1
        "Incremental Sources"), changed or not; tombstoned rows are deletions, not sightings."""
        keys = [row.key for row in rows if not row.tombstone]
        if not keys:
            return
        async with self._sessions() as session, session.begin():
            await scope_to_domain(session, domain_id)
            await SourceWrittenRepository(
                session, domain_id, writes=self._runtime.settings.writes
            ).seen(
                source.id,
                report.target,
                mapper.kind.id,
                keys,
                run_id=report.run_id,
                at=self._clock(),
            )

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
        watermark_column = source.watermark_column
        checked = False
        # Keys seen in the rows of a Source without fixed columns (JSON records): the mapped
        # columns are checked against all of them once the rows are read. Expressions are
        # compiled over all of them too, so such rows are mapped once every row was read.
        seen: dict[str, None] = {}
        held: list[tuple[str, Row]] = []
        async for locator, row in connector.rows(since):
            if not connector.columns:
                seen.update(dict.fromkeys(row))
            elif not checked:
                mapper.check_columns(connector.columns)
                mapper.use_columns(connector.columns)
                checked = True
            report.rows_read += 1
            if watermark_column is not None and row.get(watermark_column) not in (None, ""):
                read.watermark = _later(read.watermark, row[watermark_column])
            if not connector.columns and mapper.has_expressions:
                held.append((locator, row))
                continue
            self._map_row(mapper, row, locator, read, report, seen)
        if seen:
            mapper.use_columns(tuple(seen))
        for locator, row in held:
            self._map_row(mapper, row, locator, read, report, seen)
        if seen:
            mapper.check_columns(tuple(seen))
        report.rows_mapped = len(read.rows)
        SOURCE_ROWS.add(report.rows_read, {"lorebase.source": source.api_name})
        return read

    def _map_row(
        self,
        mapper: RowMapper,
        row: Row,
        locator: str,
        read: _Read,
        report: SyncReport,
        seen: Mapping[str, None],
    ) -> None:
        try:
            read.rows.append(mapper.map(row, locator))
        except RowError as error:
            report.rows_failed += 1
            if len(report.errors) < self._settings.error_samples:
                report.errors.append(error.as_dict())
            if report.rows_failed > self._settings.max_row_errors:
                if seen:  # a mapped column no row had explains the failures better
                    mapper.check_columns(tuple(seen))
                raise SourceSyncError(
                    f"more than {self._settings.max_row_errors} rows failed mapping; nothing "
                    "was committed",
                    {"rows_failed": report.rows_failed},
                ) from None

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
        origin = _source_origin(source)
        owned = await rows.record_keys_with_origin(mapper.kind.id, **origin)
        foreign = await _setters(rows, mapper.kind.id, source)
        links: dict[str, dict[str, frozenset[str]]] = {}
        owned_links: dict[str, frozenset[tuple[str, str]]] = {}
        for relation_id in mapper.relations:
            by_from: dict[str, set[str]] = defaultdict(set)
            async for link_page in rows.stream_links(relation_id, batch=batch):
                for link in link_page:
                    by_from[link.from_key].add(link.to_key)
            links[relation_id] = {key: frozenset(to) for key, to in by_from.items()}
            owned_links[relation_id] = frozenset(
                await rows.link_pairs_with_origin(relation_id, **origin)
            )
        return CurrentState(
            records=records,
            owned=frozenset(owned),
            links=links,
            foreign=foreign,
            owned_links=owned_links,
        )

    async def _written(
        self,
        session: AsyncSession,
        domain_id: str,
        mapper: RowMapper,
        source: Source,
        report: SyncReport,
    ) -> WrittenState:
        """The Source's last-written state, with the pending state of its open Proposal over
        it (the Proposal's branch holds what that state describes)."""
        repository = SourceWrittenRepository(
            session, domain_id, writes=self._runtime.settings.writes
        )
        elements = (mapper.kind.id, *mapper.relations)
        state = {
            element: await repository.current(source.id, report.target, element)
            for element in elements
        }
        preserve: dict[str, tuple[str, ...]] = {}
        proposal = await self._open_proposal(session, domain_id, report.branch, report.target)
        if proposal is not None:
            for element in elements:
                pending = await repository.pending(proposal.id, element)
                state[element].update(pending)
                if element == mapper.kind.id:
                    preserve = {key: row.preserve for key, row in pending.items() if row.preserve}
        return WrittenState(
            records=state[mapper.kind.id],
            links={relation_id: state[relation_id] for relation_id in mapper.relations},
            preserve=preserve,
        )

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
            entries = {e.id: e for c in changes for e in c.entries}
            part = f" (batch {index} of {len(planned)})" if len(planned) > 1 else ""
            try:
                async with self._sessions() as session, session.begin():
                    result = await commit(
                        session,
                        domain_id,
                        report.branch,
                        [Change(c.change, c.attribution()) for c in changes],
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

    def _detector(self, domain: DomainRow) -> Detector | None:
        if self._reconciliation is None:
            return None
        options = effective_options(self._reconciliation, domain.settings)
        if not options.enabled or not options.triggers.source_sync:
            return None
        return Detector(
            options, principal=self._principal, clock=self._clock, semantic=self._semantic
        )

    async def _reconcile(
        self,
        session: AsyncSession,
        domain: DomainRow,
        schema: Schema,
        source: Source,
        report: SyncReport,
        changes: tuple[RowMapper, Delta],
        proposal_id: str,
    ) -> None:
        """Raise the candidates of the run's changes (module doc, step 5)."""
        detector = self._detector(domain)
        mapper, delta = changes
        if detector is None or not report.revisions:
            return
        await detector.load_tags(
            session, schema, domain_id=domain.id, branch=report.target, runtime=self._runtime
        )
        raised = await sync_disagreements(
            detector,
            session,
            self._runtime,
            mapper.kind,
            delta,
            owned=mapper.owned_attributes,
            domain_id=domain.id,
            branch=report.branch,
            target=report.target,
            waits=source.target_branch_policy.merge_policy is SourcePolicy.PROPOSAL,
        )
        linked = [
            Raised(r.draft.model_copy(update={"proposal_id": proposal_id}), r.attribute)
            for r in raised
        ]
        outcome = await detector.store(session, domain.id, linked)
        for name, count in outcome.counts.items():
            report.counts[f"reconciliation_{name}"] = count

    async def _propose(
        self,
        domain: DomainRow,
        schema: Schema,
        source: Source,
        report: SyncReport,
        changes: tuple[RowMapper, Delta] | None = None,
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
            delta = None if changes is None else changes[1]
            if not report.revisions and (proposal is None or not automatic):
                # Nothing new: an open Proposal is left as it is (a no-op run). With no
                # Proposal the branch holds the target's values: the state is recorded now.
                if delta is not None:
                    await self._keep_state(session, domain.id, source, report, delta, proposal)
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
            if delta is not None and proposal.status is ProposalStatus.OPEN:
                await self._keep_state(session, domain.id, source, report, delta, proposal)
            if changes is not None:
                await self._reconcile(session, domain, schema, source, report, changes, proposal.id)
            if proposal.status is not ProposalStatus.OPEN:
                report.proposal_status = proposal.status.value
                return
            if delta is not None:
                rules = edit_rules(schema, source)
                await self._resolve_local_edits(
                    session, domain.id, proposal, policy, delta, source, rules
                )
            result = await self._merge(session, domain, proposal, policy, mode, report)
            report.merge_status = result.attempt.status.value
            if result.computation is not None:
                report.conflicts = result.computation.open_count
            final = await proposals.get(proposal.id)
            report.proposal_status = None if final is None else final.status.value
            if delta is not None:
                await self._audit_local_edits(session, domain.id, source, report, delta)

    async def _keep_state(
        self,
        session: AsyncSession,
        domain_id: str,
        source: Source,
        report: SyncReport,
        delta: Delta,
        proposal: ProposalRow | None,
    ) -> None:
        """Stage the run's last-written state in the open Proposal, or, without one, record
        it as the current state (module doc, step 5)."""
        if not delta.states:
            return
        ref = await RefRepository(session, domain_id).require(report.branch)
        rows = [draft.row(ref.head, report.run_id) for draft in delta.states]
        repository = SourceWrittenRepository(
            session, domain_id, writes=self._runtime.settings.writes
        )
        keep = None if proposal is None else proposal.id
        await repository.discard_others(source.id, report.target, keep)
        if proposal is None:
            await repository.record(source.id, report.target, rows, seen_at=self._clock())
        else:
            await repository.stage(proposal.id, source.id, report.target, rows)

    async def _resolve_local_edits(
        self,
        session: AsyncSession,
        domain_id: str,
        proposal: ProposalRow,
        policy: MergePolicy,
        delta: Delta,
        source: Source,
        rules: EditRules,
    ) -> None:
        """Resolve the local-edit Conflicts a `keep` Source decides (design 9.1, 6.10): those
        of this run and, decided again, those its open Proposal already carried."""
        partition = kind_partition(source.target_kind)
        decided: dict[tuple[str, str | None, str | None], Outcome] = {}
        if rules.mode is LocalEdits.KEEP:
            decided.update(await self._redecided(session, domain_id, proposal, source, rules))
        decided.update(
            ((partition, edit.key, edit.attribute), edit.outcome)
            for edit in delta.local_edits
            if edit.preserved
        )
        decided = {
            found: outcome
            for found, outcome in decided.items()
            if outcome in (Outcome.KEPT, Outcome.OVERWRITTEN)
        }
        if not decided:
            return
        service = MergeService(session, domain_id, runtime=self._runtime, clock=self._clock)
        preview = await service.recompute(
            MergeRequest(proposal=proposal, actor=self._principal, policy=policy)
        )
        if preview.computation is None:
            return
        now = self._clock()
        rows: list[ResolutionRow] = []
        for state in preview.computation.conflicts:
            conflict = state.conflict
            found = (conflict.partition, conflict.key, conflict.attribute)
            outcome = decided.get(found)
            if not state.open or outcome is None:
                continue
            choice = ResolutionChoice.OURS if outcome is Outcome.KEPT else ResolutionChoice.THEIRS
            resolution = Resolution(fingerprint=conflict.fingerprint, choice=choice)
            rows.append(
                ResolutionRow(
                    fingerprint=conflict.fingerprint,
                    resolution=resolution.model_dump(mode="json", exclude={"resolver"}),
                    resolved_by=LOCAL_EDITS_RESOLVER,
                    at=now,
                )
            )
        await ResolutionRepository(session, domain_id).upsert_many(proposal.id, rows)

    async def _redecided(
        self,
        session: AsyncSession,
        domain_id: str,
        proposal: ProposalRow,
        source: Source,
        rules: EditRules,
    ) -> dict[tuple[str, str | None, str | None], Outcome]:
        """The local edits the open Proposal preserves, decided again with who set the
        target's current values: a person may have edited a kept value again, which makes a
        new Conflict a run that meets no upstream change would otherwise leave open. One
        decision per preserved Attribute plus one for the whole Record (a `DV2`); a decision
        matching no open Conflict is ignored."""
        settings = self._runtime.settings
        written = SourceWrittenRepository(session, domain_id, writes=settings.writes)
        preserved = [
            row.pending
            for row in await written.preserved(proposal.id)
            if row.pending.element == source.target_kind
        ]
        if not preserved:
            return {}
        target = MaterializedRepository(
            session, domain_id, proposal.target_ref, writes=settings.writes
        )
        setters = await _setters(target, source.target_kind, source)
        partition = kind_partition(source.target_kind)
        found: dict[tuple[str, str | None, str | None], Outcome] = {}
        for pending in preserved:
            for attribute in (None, *pending.preserve):
                setter = None if attribute is None else setters.get((pending.key, attribute))
                found[(partition, pending.key, attribute)] = rules.outcome(attribute, setter)
        return found

    async def _audit_local_edits(
        self,
        session: AsyncSession,
        domain_id: str,
        source: Source,
        report: SyncReport,
        delta: Delta,
    ) -> None:
        """One `source.local_edit_conflict` entry per local edit; never values."""
        if not delta.local_edits:
            return
        mode = source.local_edit_mode.value
        now = self._clock()
        await AuditRepository(session).append_many(
            [
                NewAuditEntry(
                    actor=self._principal,
                    action=LOCAL_EDIT_ACTION,
                    domain_id=domain_id,
                    target=report.proposal_id,
                    details={
                        "source": source.id,
                        "run": report.run_id,
                        "proposal": report.proposal_id,
                        "kind": source.target_kind,
                        "key": edit.key,
                        "attribute": edit.attribute,
                        "origin_type": None if edit.setter is None else edit.setter.origin_type,
                        "actor": None if edit.setter is None else edit.setter.actor,
                        "local_edits": mode,
                        "outcome": edit.outcome.value,
                    },
                    at=now,
                )
                for edit in delta.local_edits
            ]
        )

    async def _merge(
        self,
        session: AsyncSession,
        domain: DomainRow,
        proposal: ProposalRow,
        policy: MergePolicy,
        mode: SourcePolicy,
        report: SyncReport,
    ) -> AttemptResult:
        service = MergeService(session, domain.id, runtime=self._runtime, clock=self._clock)
        request = MergeRequest(
            proposal=proposal,
            actor=self._principal,
            policy=policy,
            idempotency_key=report.run_id,
            # The operator's mode; the merge service applies the Domain's setting only when
            # it is stricter.
            reconciliation_check=None
            if self._reconciliation is None
            else self._reconciliation.merge_check,
        )
        automatic = mode in (SourcePolicy.AUTO_WHEN_CLEAN, SourcePolicy.AUTO_WITH_AUTHORITY)
        if automatic:
            target = await RefRepository(session, domain.id).require(report.target)
            if target.protected and not self._settings.auto_merge_protected:
                automatic = False
            elif target.protected:
                # No person reviewed it: rules naming who must approve hold it for review.
                rules = effective_rules(domain.settings, self._approval_rules).requiring
                if rules:
                    report.merge_blocked = HELD_BY_APPROVALS
                    report.blocking_rules = list(rules)
                    automatic = False
        if automatic:
            return await service.merge(request)
        return await service.recompute(request)
