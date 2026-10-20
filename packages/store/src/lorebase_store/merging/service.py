"""Merge service: the core merge over the store, merge attempts and merge commits (design 6.8).

`MergeService` runs `lorebase_core.merge` for a Proposal over the Domain's tree nodes, objects
and Revision graph, with the materialized head of the target branch as index view and the
source branch's materialized Lineage as provenance. Every run is stored as a merge attempt
with its Conflicts (the previous open attempt of the Proposal becomes `stale`); resolutions
are read from `conflict_resolutions` and matched by fingerprint, so an attempt recomputed
against moved heads keeps the resolutions whose Conflicts did not change.

`merge` runs under the target branch lock and, when no Conflict remains, writes the outcome
like the commit pipeline (design 6.6): objects, nodes, the merge Revision with parents
`[target head, source head]`, changeset and provenance, materialization of the changeset on the
target branch, compare-and-swap of the ref, audit entry and outbox event. A fast-forward moves
the ref to the source head and materializes the difference; an up-to-date merge writes
nothing. Everything runs in the caller's transaction.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from lorebase_core.canonical import hex
from lorebase_core.merge import (
    MergedTree,
    MergeInput,
    MergePolicy,
    MergeResult,
    Resolution,
    tree_schema_loader,
)
from lorebase_core.merge.objects import load_object
from lorebase_core.model import (
    IdPrefix,
    MergeResolutionOrigin,
    ProvenanceEntry,
    Schema,
    new_id,
    new_ulid,
)
from lorebase_core.revision import Author, ChangesetEntry, Origin, Revision
from lorebase_core.tree import PARTITION_SCHEMA, Tree, TreeParams
from lorebase_store.commit.errors import (
    BranchLockTimeoutError,
    BranchNotFoundError,
    StaleHeadError,
)
from lorebase_store.commit.pipeline import (
    COMMITTED_EVENT,
    _Prepared,
    _record_events,
    _store_revision,
    utc_now,
)
from lorebase_store.materialize.apply import MaterializedChange, apply_changeset
from lorebase_store.merging.attempts import (
    AttemptComputation,
    ConflictState,
    complete_provenance,
    compute_attempt,
    decisions_of,
)
from lorebase_store.merging.lineage import BranchLineage
from lorebase_store.models.enums import MergeAttemptStatus, ProposalStatus, RefKind
from lorebase_store.nodestore import PgNodeStore
from lorebase_store.objectstore import EncodedObject, SessionObjectStore
from lorebase_store.repositories.domains import DomainRepository
from lorebase_store.repositories.events import OutboxRepository
from lorebase_store.repositories.index_view import MaterializedIndexView
from lorebase_store.repositories.merges import (
    ConflictRepository,
    ConflictRow,
    MergeAttemptRepository,
    MergeAttemptRow,
    ResolutionRepository,
    ResolutionRow,
)
from lorebase_store.repositories.proposals import ProposalRepository, ProposalRow
from lorebase_store.repositories.provenance import ProvenanceRow
from lorebase_store.repositories.refs import (
    LockTimeoutError,
    RefMovedError,
    RefRepository,
    RefRow,
)
from lorebase_store.repositories.revisions import PgRevisionGraph, RevisionRow
from lorebase_store.repositories.scope import scope_to_domain
from lorebase_store.runtime import StoreRuntime

MERGE_ORIGIN = "merge"
"""Origin type of merge Revisions and of the merge's own provenance entry."""
MERGE_REASON = "proposal.merge"
"""Ref log reason of a merge."""
ATTEMPT_PREFIX = "mga"
CONFLICTS_RESULT = "conflicts"
REJECTED_DETAIL = (
    "The merge reported this Conflict again after applying its resolution; choose another."
)

Clock = Callable[[], datetime]


class ProposalBranchMissingError(BranchNotFoundError):
    """The source or target branch of a Proposal no longer exists."""

    def __init__(self, side: str, name: str) -> None:
        super().__init__(f"the {side} branch {name!r} of the Proposal does not exist")
        self.side = side
        self.name = name


@dataclass(frozen=True)
class MergeRequest:
    """What to merge and how: the Proposal, who runs it and the merge policy."""

    proposal: ProposalRow
    actor: str
    policy: MergePolicy = field(default_factory=MergePolicy)
    message: str | None = None
    idempotency_key: str | None = None
    #: Delete the source branch after a successful merge (auto-close).
    delete_source: bool = False


@dataclass(frozen=True)
class AttemptResult:
    """The current attempt of a Proposal.

    `computation` is set when this call ran the merge (otherwise the stored attempt was still
    current); `previous` is the attempt it superseded. After a successful `merge`, `head` is
    the new head of the target branch.
    """

    attempt: MergeAttemptRow
    computation: AttemptComputation | None = None
    previous: MergeAttemptRow | None = None
    head: bytes | None = None
    source_deleted: bool = False

    @property
    def merged(self) -> bool:
        return self.attempt.status is MergeAttemptStatus.MERGED

    @property
    def stale_previous(self) -> bool:
        """Whether a head moved since the previous attempt."""
        previous = self.previous
        return previous is not None and (
            previous.ours != self.attempt.ours or previous.theirs != self.attempt.theirs
        )


@dataclass
class _Commit:
    merged: MergedTree
    changeset: list[ChangesetEntry]
    bodies: dict[tuple[str, str], Mapping[str, Any] | None]
    schema: Schema
    schema_hash: bytes | None
    refresh_kinds: list[str]
    provenance: list[ProvenanceRow]


@dataclass
class _Computed:
    computation: AttemptComputation
    nodes: dict[bytes, bytes]
    objects: list[EncodedObject]
    commit: _Commit | None


class MergeService:
    """Merge attempts and merge commits of one Domain, in the caller's transaction."""

    def __init__(
        self,
        session: AsyncSession,
        domain_id: str,
        *,
        runtime: StoreRuntime,
        clock: Clock = utc_now,
        retention: int = 5,
    ) -> None:
        self._session = session
        self._domain = domain_id
        self._runtime = runtime
        self._clock = clock
        self._retention = retention

    async def current(self, request: MergeRequest) -> AttemptResult:
        """The latest attempt, recomputed when there is none or a head moved since."""
        await scope_to_domain(self._session, self._domain)
        refs = RefRepository(self._session, self._domain)
        target = await _branch(refs, request.proposal.target_ref, "target")
        source = await _branch(refs, request.proposal.source_ref, "source")
        latest = await MergeAttemptRepository(self._session, self._domain).latest(
            request.proposal.id
        )
        if (
            latest is not None
            and latest.status in (MergeAttemptStatus.CONFLICTED, MergeAttemptStatus.RESOLVED)
            and latest.ours == target.head
            and latest.theirs == source.head
        ):
            return AttemptResult(attempt=latest)
        return await self._run(request, target, source, commit=False)

    async def recompute(self, request: MergeRequest) -> AttemptResult:
        """Run the merge with the stored resolutions and store the attempt (no commit)."""
        await scope_to_domain(self._session, self._domain)
        refs = RefRepository(self._session, self._domain)
        target = await _branch(refs, request.proposal.target_ref, "target")
        source = await _branch(refs, request.proposal.source_ref, "source")
        return await self._run(request, target, source, commit=False)

    async def merge(self, request: MergeRequest) -> AttemptResult:
        """Merge under the target branch lock: commit when clean, else store the attempt.

        Raises `BranchLockTimeoutError`, `ProposalBranchMissingError` or `StaleHeadError`.
        """
        await scope_to_domain(self._session, self._domain)
        refs = RefRepository(self._session, self._domain)
        timeout = self._runtime.settings.commit.lock_timeout_ms
        try:
            await refs.lock(request.proposal.target_ref, timeout_ms=timeout)
        except LockTimeoutError as error:
            raise BranchLockTimeoutError(str(error)) from error
        target = await _branch(refs, request.proposal.target_ref, "target")
        source = await _branch(refs, request.proposal.source_ref, "source")
        return await self._run(request, target, source, commit=True)

    async def abandon(self, proposal_id: str) -> int:
        """Mark the open attempts of a closed Proposal `abandoned`."""
        await scope_to_domain(self._session, self._domain)
        return await MergeAttemptRepository(self._session, self._domain).supersede(
            proposal_id, status=MergeAttemptStatus.ABANDONED
        )

    # Run ------------------------------------------------------------------------------

    async def _run(
        self, request: MergeRequest, target: RefRow, source: RefRow, *, commit: bool
    ) -> AttemptResult:
        session, domain_id = self._session, self._domain
        settings = self._runtime.settings
        domain = await DomainRepository(session).require(domain_id)
        stored = await ResolutionRepository(session, domain_id).of(request.proposal.id)
        resolutions = [_resolution(row) for row in stored]
        created_at = self._clock()
        attempt_id = f"{ATTEMPT_PREFIX}_{new_ulid()}"
        runtime = self._runtime

        def compute(sync: Session) -> _Computed:
            store = PgNodeStore(
                sync,
                domain_id,
                cache=runtime.node_cache,
                prefetch_batch=settings.nodes.prefetch_batch,
            )
            objects = SessionObjectStore(sync, domain_id, batch=settings.nodes.prefetch_batch)
            graph = PgRevisionGraph(sync, domain_id)
            index = MaterializedIndexView(
                sync, domain_id, target.name, batch=settings.commit.prefetch_batch
            )
            merge_input = MergeInput(
                store=store,
                objects=objects,
                graph=graph,
                revision_root=graph.root,
                ours=target.head,
                theirs=source.head,
                index_view=lambda _head: index,
                provenance=BranchLineage(sync, domain_id, source.name),
                policy=request.policy,
                tree_params=TreeParams(domain.leaf_size),
                schema_rules=runtime.schema.rules(),
            )
            computation = compute_attempt(merge_input, resolutions)
            prepared = None
            outcome = computation.outcome
            if (
                commit
                and isinstance(outcome, MergedTree)
                and outcome.result is not MergeResult.UP_TO_DATE
            ):
                prepared = _prepare_commit(
                    store, objects, computation, outcome, request, created_at
                )
            return _Computed(computation, store.take_pending(), objects.take_pending(), prepared)

        computed = await session.run_sync(compute)
        computation = computed.computation
        head: bytes | None = None
        source_deleted = False
        if commit and computation.clean:
            head = await self._commit(request, target, source, computed, created_at)
            if request.delete_source and source.name != target.name:
                source_deleted = await RefRepository(session, domain_id).delete(source.name)
        status = (
            MergeAttemptStatus.MERGED
            if head is not None
            else MergeAttemptStatus.RESOLVED
            if computation.clean
            else MergeAttemptStatus.CONFLICTED
        )
        attempts = MergeAttemptRepository(session, domain_id)
        previous = await attempts.latest(request.proposal.id)
        await attempts.supersede(request.proposal.id, status=MergeAttemptStatus.STALE)
        outcome = computation.outcome
        attempt = MergeAttemptRow(
            id=attempt_id,
            proposal_id=request.proposal.id,
            base=None if outcome.virtual_base else outcome.base,
            ours=target.head,
            theirs=source.head,
            status=status,
            created_at=created_at,
            virtual_base=outcome.virtual_base,
            result=outcome.result.value if isinstance(outcome, MergedTree) else CONFLICTS_RESULT,
            policy=request.policy.model_dump(mode="json", exclude_defaults=True),
            created_by=request.actor,
            outcome=_summary(computation),
            revision=head,
            idempotency_key=request.idempotency_key if commit else None,
        )
        await attempts.insert(attempt)
        by_fingerprint = {row.fingerprint: row for row in stored}
        await ConflictRepository(session, domain_id).insert_many(
            attempt_id, [_conflict_row(state, by_fingerprint) for state in computation.conflicts]
        )
        await attempts.prune(request.proposal.id, keep=self._retention)
        changes: dict[str, Any] = {"checks": _checks(request.proposal, attempt, computation)}
        if head is not None:
            changes.update(status=ProposalStatus.MERGED, merged_revision=head)
        await ProposalRepository(session, domain_id).update(
            request.proposal.id, at=created_at, **changes
        )
        return AttemptResult(
            attempt=attempt,
            computation=computation,
            previous=previous,
            head=head,
            source_deleted=source_deleted,
        )

    async def _commit(
        self,
        request: MergeRequest,
        target: RefRow,
        source: RefRow,
        computed: _Computed,
        created_at: datetime,
    ) -> bytes:
        """Write a clean outcome to the target branch; returns its new head."""
        prepared = computed.commit
        if prepared is None:  # up to date: the target already contains the source
            return target.head
        session, domain_id = self._session, self._domain
        settings = self._runtime.settings
        merged = prepared.merged
        changes = [
            MaterializedChange(entry, prepared.bodies[(entry.partition, entry.key)])
            for entry in prepared.changeset
        ]
        message = request.message or _message(request.proposal)
        stored: RevisionRow | None = None
        if merged.result is MergeResult.FAST_FORWARD:
            new_head, at = source.head, created_at
        else:
            revision = Revision(
                parents=(target.head, source.head),
                root=merged.root,
                domain=domain_id,
                author=Author(principal=request.actor),
                message=message,
                origin=Origin(type=MERGE_ORIGIN, ref=request.proposal.id),
                created_at=created_at,
                stats=merged.stats,
            )
            stored = await _store_revision(
                session,
                self._runtime,
                revision,
                _Prepared(
                    root=merged.root,
                    changeset=prepared.changeset,
                    bodies=prepared.bodies,
                    objects=computed.objects,
                    nodes=computed.nodes,
                    schema=prepared.schema,
                    schema_hash=prepared.schema_hash,
                ),
                prepared.provenance,
            )
            new_head, at = stored.id, stored.created_at
        await apply_changeset(
            session,
            domain_id,
            target.name,
            revision_id=new_head,
            at=at,
            changes=changes,
            schema=prepared.schema,
            schema_hash=prepared.schema_hash,
            refresh_kinds=prepared.refresh_kinds,
            settings=settings,
            expression_limits=self._runtime.schema.expression_limits,
        )
        refs = RefRepository(session, domain_id)
        try:
            await refs.compare_and_swap(
                target.name,
                expected_head=target.head,
                expected_version=target.version,
                new_head=new_head,
                actor=request.actor,
                at=created_at,
                reason=MERGE_REASON,
            )
        except RefMovedError as error:
            current = await refs.get(target.name)
            raise StaleHeadError(
                target.name, target.head, None if current is None else current.head
            ) from error
        if stored is None:
            await OutboxRepository(session).enqueue(
                domain_id,
                COMMITTED_EVENT,
                {
                    "domain": domain_id,
                    "branch": target.name,
                    "revision": hex(new_head),
                    "fast_forward": True,
                    "changes": merged.stats.changes,
                    "message": message,
                },
            )
        else:
            await _record_events(
                session, domain_id, target.name, stored, prepared.changeset, message
            )
        return new_head


async def _branch(refs: RefRepository, name: str, side: str) -> RefRow:
    ref = await refs.get(name)
    if ref is None or ref.kind is not RefKind.BRANCH:
        raise ProposalBranchMissingError(side, name)
    return ref


def _resolution(row: ResolutionRow) -> Resolution:
    return Resolution.model_validate(
        {**row.resolution, "fingerprint": row.fingerprint, "resolver": row.resolved_by}
    )


def _message(proposal: ProposalRow) -> str:
    return f"Merge {proposal.source_ref} into {proposal.target_ref}: {proposal.title}"


def _prepare_commit(
    store: PgNodeStore,
    objects: SessionObjectStore,
    computation: AttemptComputation,
    merged: MergedTree,
    request: MergeRequest,
    created_at: datetime,
) -> _Commit:
    """Changeset with provenance, object bodies and schema of a clean outcome."""
    provenance: list[ProvenanceRow] = []
    changeset = list(merged.changes)
    if merged.result is MergeResult.MERGED:
        own = ProvenanceRow(
            id=new_id(IdPrefix.PROVENANCE),
            origin_type=MERGE_ORIGIN,
            origin_ref=Origin(type=MERGE_ORIGIN, ref=request.proposal.id).model_dump(),
            actor=request.actor,
            at=created_at,
        )
        decisions = decisions_of(computation, merged.auto_resolutions)
        entries = {
            d.fingerprint: ProvenanceEntry(
                id=new_id(IdPrefix.PROVENANCE),
                origin=MergeResolutionOrigin(
                    proposal_id=request.proposal.id,
                    conflict_id=d.fingerprint,
                    resolver=d.resolver or request.actor,
                ),
                actor=d.resolver if d.resolver and ":" not in d.resolver else request.actor,
                at=created_at,
            )
            for d in decisions
        }
        changeset, used = complete_provenance(
            changeset, objects, decisions, {fp: e.id for fp, e in entries.items()}, own.id
        )
        provenance = [own, *(ProvenanceRow.from_entry(entries[fp]) for fp in sorted(used))]
    bodies: dict[tuple[str, str], Mapping[str, Any] | None] = {
        (entry.partition, entry.key): None
        if entry.new_hash is None
        else load_object(objects, entry.new_hash)
        for entry in changeset
    }
    schema = tree_schema_loader(store, objects)(merged.root)
    schema_keys = [entry.key for entry in changeset if entry.partition == PARTITION_SCHEMA]
    schema_hash = (
        Tree.load(store, merged.root).partitions[PARTITION_SCHEMA].root if schema_keys else None
    )
    return _Commit(
        merged=merged,
        changeset=changeset,
        bodies=bodies,
        schema=schema,
        schema_hash=schema_hash,
        refresh_kinds=sorted(key for key in schema_keys if key in schema.kinds),
        provenance=provenance,
    )


def _summary(computation: AttemptComputation) -> dict[str, Any]:
    outcome = computation.outcome
    summary: dict[str, Any] = {
        "open": computation.open_count,
        "resolved": computation.resolved_count,
        "auto_resolutions": [a.model_dump(mode="json") for a in outcome.auto_resolutions],
        "unmatched_resolutions": list(computation.unmatched),
    }
    if isinstance(outcome, MergedTree):
        summary["stats"] = outcome.stats.model_dump()
    return summary


def _conflict_row(state: ConflictState, stored: Mapping[str, ResolutionRow]) -> ConflictRow:
    conflict = state.conflict
    payload = conflict.model_dump(mode="json")
    payload["members"] = list(state.members)
    if state.rejected:
        payload["details"] = {**payload.get("details", {}), "rejected": REJECTED_DETAIL}
    row = stored.get(conflict.fingerprint)
    resolved = not state.open and state.resolution is not None
    return ConflictRow(
        fingerprint=conflict.fingerprint,
        code=conflict.kind.value,
        partition=conflict.partition,
        element_key=conflict.key,
        attribute_id=conflict.attribute,
        payload=payload,
        member_count=conflict.count,
        resolution=state.resolution.model_dump(mode="json")
        if resolved and state.resolution
        else None,
        resolved_by=row.resolved_by if resolved and row else None,
        resolved_at=row.at if resolved and row else None,
    )


def _checks(
    proposal: ProposalRow, attempt: MergeAttemptRow, computation: AttemptComputation
) -> dict[str, Any]:
    """Proposal checks with the merge preview of the latest attempt (design 10.3)."""
    checks = dict(proposal.checks or {})
    checks["merge"] = {
        "attempt": attempt.id,
        "result": attempt.result,
        "status": attempt.status.value,
        "open_conflicts": computation.open_count,
        "resolved_conflicts": computation.resolved_count,
        "ours": hex(attempt.ours),
        "theirs": hex(attempt.theirs),
        "at": attempt.created_at.isoformat(),
    }
    return checks
