"""The commit pipeline (design 6.6): every write goes through one transaction.

1. take the branch advisory lock; read head `H` and the ref version;
2. validate the changes against the materialized head of `H` (bulk-prefetched `IndexView`);
   schema changes add their data migrations; values of vault Attributes are validated, then
   replaced by vault references (`lorebase_store.vault`, under the Domain's vault lock held
   shared to the end of the transaction) before the core rules run;
3. canonicalize and hash new objects; insert them, new vault rows and vault lineage, with
   `ON CONFLICT DO NOTHING`;
4. apply the changes to the tree by path copying; insert new nodes;
5. insert the Revision with parent `H`, its changeset and provenance entries;
6. apply the changeset to the materialized head; when the schema changed, per-Attribute
   indexes that would reject the new rows are dropped first and missing ones created after
   (`lorebase_store.indexes`);
7. compare-and-swap the ref from `H` to the new Revision;
8. write the audit entry and the outbox event;
9. commit.

Steps 2 to 4 run synchronously inside `AsyncSession.run_sync` (the core validation and tree
code are synchronous); their reads go through prefetching layers, so a batch costs a few
queries per tree level and per kind of question, not one per element.

`commit` runs in the caller's transaction when one is open (the ref then moves when the
caller commits) and in its own transaction otherwise. After a failure the transaction must be
rolled back; nothing of the commit is visible.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from lorebase_core.canonical import ObjectType, hex
from lorebase_core.expr.sensitivity import lowered_tags, schema_tags, with_tags
from lorebase_core.model import (
    Domain,
    IdPrefix,
    ProvenanceEntry,
    Schema,
    Sensitivity,
    new_id,
    validate_schema,
)
from lorebase_core.revision import (
    Author,
    ChangesetEntry,
    Origin,
    Revision,
    changeset_between,
    changeset_stats,
)
from lorebase_core.tree import (
    DEFAULT_LEAF_SIZE,
    PARTITION_SCHEMA,
    InMemoryNodeStore,
    Tree,
    TreeChange,
    TreeParams,
    kind_partition,
    link_key,
    relation_partition,
    tree_apply,
)
from lorebase_core.validate import (
    DataChange,
    Issue,
    LinkDelete,
    LinkUpsert,
    RecordDelete,
    RecordUpsert,
    evaluate_changeset,
)
from lorebase_store.auth.audit import AuditRepository, NewAuditEntry
from lorebase_store.commit.changes import (
    Change,
    CommitResult,
    SchemaChange,
    SchemaElementModel,
)
from lorebase_store.commit.errors import (
    BranchLockTimeoutError,
    BranchMaterializingError,
    BranchNotFoundError,
    BranchNotMaterializedError,
    SensitivityLoweredError,
    StaleHeadError,
    ValidationFailedError,
)
from lorebase_store.commit.migrations import MigrationPlan, plan_migrations
from lorebase_store.commit.schema import (
    apply_schema_changes,
    element_key,
    load_compiled,
    object_type_of,
)
from lorebase_store.indexes import BranchIndexes
from lorebase_store.materialize.apply import MaterializedChange, apply_changeset
from lorebase_store.materialize.rows import (
    ELEMENT_PROVENANCE_KEY,
    PartitionType,
    changed_attributes,
    parse_partition,
    split_link_key,
)
from lorebase_store.materialize.sensitivity import default_branch_schema
from lorebase_store.materialize.stale import queue_after_tagging
from lorebase_store.models.enums import Materialization, RefKind
from lorebase_store.nodestore import NodeRepository, PgNodeStore
from lorebase_store.objectstore import EncodedObject, PgObjectStore, encode_object
from lorebase_store.repositories.changesets import ChangesetRepository
from lorebase_store.repositories.domains import DomainRepository, DomainRow
from lorebase_store.repositories.events import OutboxRepository
from lorebase_store.repositories.index_view import MaterializedIndexView
from lorebase_store.repositories.materialized import MaterializedRepository
from lorebase_store.repositories.provenance import ProvenanceRepository, ProvenanceRow
from lorebase_store.repositories.refs import LockTimeoutError, RefMovedError, RefRepository
from lorebase_store.repositories.revisions import RevisionRepository, RevisionRow
from lorebase_store.repositories.scope import scope_to_domain
from lorebase_store.runtime import StoreRuntime, default_runtime
from lorebase_store.storage import constraints_only
from lorebase_store.telemetry import COMMIT_DURATION
from lorebase_store.vault.store import (
    Lineage,
    Move,
    VaultLockTimeoutError,
    VaultRow,
    insert_lineage,
    insert_rows,
    mark_moved,
    tokenize,
)

COMMIT_ACTION = "revision.commit"
COMMITTED_EVENT = "revision.committed"
DOMAIN_ORIGIN = "domain.create"

Clock = Callable[[], datetime]
ProvenanceSpec = str | Mapping[str, str] | None


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class _Prepared:
    """Outcome of steps 2 to 4."""

    root: bytes
    changeset: list[ChangesetEntry]
    bodies: dict[tuple[str, str], Mapping[str, Any] | None]
    objects: list[EncodedObject]
    nodes: dict[bytes, bytes]
    schema: Schema
    schema_hash: bytes | None
    refresh_kinds: list[str] = field(default_factory=list)
    provenance: list[ProvenanceEntry] = field(default_factory=list)
    derived: list[DataChange] = field(default_factory=list)
    vault_rows: list[VaultRow] = field(default_factory=list)
    vault_lineage: list[Lineage] = field(default_factory=list)
    vault_moved: list[Move] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Element:
    """One element written to the tree: partition, key, encoded object or `None`."""

    partition: str
    key: str
    encoded: EncodedObject | None
    provenance: ProvenanceSpec = None


async def commit(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    changes: Iterable[DataChange | Change],
    *,
    author: Author,
    message: str,
    origin: Origin,
    schema_changes: Sequence[SchemaChange] = (),
    provenance: Sequence[ProvenanceEntry] = (),
    expected_head: bytes | None = None,
    runtime: StoreRuntime | None = None,
    clock: Clock = utc_now,
    allowed_lowering: frozenset[Sensitivity] = frozenset(),
) -> CommitResult:
    """Commit data and schema changes to `branch` as one Revision.

    Schema changes that remove sensitivity tags (or sensitive Attributes, Kinds, Relations)
    raise `SensitivityLoweredError` for every tag not in `allowed_lowering` (the tags the
    caller is cleared for), compared under the branch lock with the head actually changed.
    Schemas and data are validated with the tags of the default branch's schema added
    (`lorebase_core.expr.sensitivity.with_tags`).

    `provenance` lists entries referenced by `Change.provenance`; changes without one are
    attributed to an entry built from `origin` and `author`. `expected_head`, when given,
    must be the current head (optimistic concurrency for API callers).

    Raises `BranchNotFoundError`, `BranchNotMaterializedError`, `BranchLockTimeoutError`,
    `StaleHeadError`, `SchemaChangeError` or `ValidationFailedError`.
    """
    request = _Request(
        domain_id=domain_id,
        branch=branch,
        changes=[c if isinstance(c, Change) else Change(c) for c in changes],
        author=author,
        message=message,
        origin=origin,
        schema_changes=list(schema_changes),
        provenance=list(provenance),
        expected_head=expected_head,
        runtime=runtime or default_runtime(),
        clock=clock,
        allowed_lowering=allowed_lowering,
    )
    began = perf_counter()
    if session.in_transaction():
        result = await _commit(session, request)
    else:
        async with session.begin():
            result = await _commit(session, request)
    COMMIT_DURATION.record(perf_counter() - began, {"origin": origin.type})
    return result


async def create_domain(
    session: AsyncSession,
    domain: Domain,
    *,
    author: Author,
    leaf_size: int = DEFAULT_LEAF_SIZE,
    message: str = "Create Domain",
    origin: Origin | None = None,
    runtime: StoreRuntime | None = None,
    clock: Clock = utc_now,
) -> CommitResult:
    """Create a Domain: its row, a root Revision holding the Domain element and the default
    branch (fully materialized) pointing at it. `leaf_size` is fixed for the Domain's life."""
    if session.in_transaction():
        return await _create_domain(
            session, domain, author, leaf_size, message, origin, runtime, clock
        )
    async with session.begin():
        return await _create_domain(
            session, domain, author, leaf_size, message, origin, runtime, clock
        )


@dataclass
class _Request:
    domain_id: str
    branch: str
    changes: list[Change]
    author: Author
    message: str
    origin: Origin
    schema_changes: list[SchemaChange]
    provenance: list[ProvenanceEntry]
    expected_head: bytes | None
    runtime: StoreRuntime
    clock: Clock
    allowed_lowering: frozenset[Sensitivity] = frozenset()


async def _commit(session: AsyncSession, request: _Request) -> CommitResult:
    settings = request.runtime.settings
    domain_id, branch = request.domain_id, request.branch
    await scope_to_domain(session, domain_id)
    domain = await DomainRepository(session).require(domain_id)
    refs = RefRepository(session, domain_id)
    try:
        await refs.lock(branch, timeout_ms=settings.commit.lock_timeout_ms)
    except LockTimeoutError as error:
        raise BranchLockTimeoutError(str(error)) from error
    ref = await refs.get(branch)
    if ref is None or ref.kind is not RefKind.BRANCH:
        raise BranchNotFoundError(f"no branch {branch!r} in Domain {domain_id!r}")
    if request.expected_head is not None and request.expected_head != ref.head:
        raise StaleHeadError(branch, request.expected_head, ref.head)
    if ref.materialization is Materialization.BUILDING:
        raise BranchMaterializingError(
            f"branch {branch!r} is being built from its tree in the background; retry when "
            "its materialization is done"
        )
    revisions = RevisionRepository(session, domain_id)
    head = await revisions.require(ref.head)
    materialized = MaterializedRepository(session, domain_id, branch, writes=settings.writes)
    compiled = await materialized.schema()
    if compiled is None:
        raise BranchNotMaterializedError(f"branch {branch!r} has no materialized head")
    created_at = request.clock()
    own = ProvenanceRow(
        id=new_id(IdPrefix.PROVENANCE),
        origin_type=request.origin.type,
        origin_ref=request.origin.model_dump(),
        actor=request.author.principal,
        at=created_at,
    )
    old_schema = load_compiled(compiled.compiled)
    reference: Schema | None = old_schema
    if branch != domain.default_branch:
        reference = await default_branch_schema(session, domain_id, runtime=request.runtime)
    reference_tags = schema_tags(reference)

    def prepare(sync: Session) -> _Prepared:
        return _prepare(
            sync, request, domain, head.root, old_schema, own.id, created_at, reference_tags
        )

    try:
        prepared = await session.run_sync(prepare)
    except VaultLockTimeoutError as error:
        raise BranchLockTimeoutError(str(error)) from error
    if not prepared.changeset:
        return CommitResult(
            revision_id=head.id,
            parents=head.parents,
            root=head.root,
            generation=head.generation,
            ref_version=ref.version,
            created_at=head.created_at,
            changeset=(),
            no_op=True,
        )
    revision = Revision(
        parents=(head.id,),
        root=prepared.root,
        domain=domain_id,
        author=request.author,
        message=request.message,
        origin=request.origin,
        created_at=created_at,
        stats=changeset_stats(prepared.changeset),
    )
    provenance_rows = [
        own,
        *(ProvenanceRow.from_entry(e) for e in request.provenance),
        *(ProvenanceRow.from_entry(e) for e in prepared.provenance),
    ]
    stored = await _store_revision(session, request.runtime, revision, prepared, provenance_rows)
    indexes = None
    if prepared.schema_hash is not None:
        indexes = BranchIndexes(
            session,
            domain_id,
            branch,
            settings=settings.indexes,
            constraints_only=constraints_only(settings),
            maintenance=request.runtime.index_maintenance,
        )
        await indexes.retire(prepared.schema)
    await apply_changeset(
        session,
        domain_id,
        branch,
        revision_id=stored.id,
        at=stored.created_at,
        changes=_materialized_changes(prepared),
        schema=prepared.schema,
        schema_hash=prepared.schema_hash,
        refresh_kinds=prepared.refresh_kinds,
        settings=settings,
        expression_limits=request.runtime.schema.expression_limits,
        runtime=request.runtime,
    )
    if indexes is not None:
        await indexes.ensure(prepared.schema)
    if prepared.schema_hash is not None and branch == domain.default_branch:
        await queue_after_tagging(
            session, domain_id, branch, old_schema, prepared.schema, runtime=request.runtime
        )
    try:
        version = await refs.compare_and_swap(
            branch,
            expected_head=ref.head,
            expected_version=ref.version,
            new_head=stored.id,
            actor=request.author.principal,
            at=created_at,
            reason=COMMIT_ACTION,
        )
    except RefMovedError as error:
        current = await refs.get(branch)
        raise StaleHeadError(branch, ref.head, None if current is None else current.head) from error
    await _record_events(session, domain_id, branch, stored, prepared.changeset, request.message)
    return CommitResult(
        revision_id=stored.id,
        parents=stored.parents,
        root=stored.root,
        generation=stored.generation,
        ref_version=version,
        created_at=stored.created_at,
        changeset=tuple(prepared.changeset),
        derived_changes=tuple(prepared.derived),
    )


def _prepare(
    sync: Session,
    request: _Request,
    domain: DomainRow,
    head_root: bytes,
    old_schema: Schema,
    own_provenance: str,
    created_at: datetime,
    reference_tags: Mapping[str, frozenset[Sensitivity]],
) -> _Prepared:
    settings = request.runtime.settings
    view = MaterializedIndexView(
        sync, request.domain_id, request.branch, batch=settings.commit.prefetch_batch
    )
    schema = old_schema
    elements: list[_Element] = []
    issues: list[Issue] = []
    plan = MigrationPlan()
    if request.schema_changes:
        schema, upserted, removed = apply_schema_changes(
            old_schema, request.schema_changes, request.domain_id
        )
        refused = lowered_tags(old_schema, schema) - request.allowed_lowering
        if refused:
            raise SensitivityLoweredError(t.value for t in refused)
        issues.extend(
            i
            for i in validate_schema(
                with_tags(schema, reference_tags), request.runtime.schema.rules()
            )
            if i.is_error
        )
        elements.extend(_schema_element(e) for e in upserted)
        elements.extend(_Element(PARTITION_SCHEMA, key, None) for key in removed)
        plan = plan_migrations(
            old_schema,
            schema,
            records_of=view.records_of_kind,
            links_of=view.links_of_relation,
            skip=[c.change.identity for c in request.changes],
            sensitive=lambda a: a.is_sensitive or a.id in reference_tags,
            actor=request.author.principal,
            at=created_at,
        )
        issues.extend(i for i in plan.issues if i.is_error)
    validated = [*plan.changes, *request.changes]
    data = [c.change for c in validated]
    view.prefetch_for(schema, data)
    vaulted = tokenize(
        sync,
        request.domain_id,
        schema,
        data,
        request.runtime.vault,
        view,
        erased_values=request.runtime.vault_settings.erased_values,
        tombstones=request.origin.type in request.runtime.vault_settings.tombstone_origins,
        lock_timeout_ms=request.runtime.vault_settings.lock_timeout_ms,
    )
    issues.extend(vaulted.issues)
    if issues:
        raise ValidationFailedError(issues)
    data = vaulted.changes
    validated = [Change(d, c.provenance) for d, c in zip(data, validated, strict=True)]
    report = evaluate_changeset(with_tags(schema, reference_tags), view, data)
    issues.extend(i for i in report.issues if i.is_error)
    if issues:
        raise ValidationFailedError(issues)
    elements.extend(_data_element(c.change, c.provenance) for c in validated)
    elements.extend(_data_element(d, None) for d in [*report.derived_changes, *plan.removals])

    store = PgNodeStore(
        sync,
        request.domain_id,
        cache=request.runtime.node_cache,
        prefetch_batch=settings.nodes.prefetch_batch,
    )
    tree_changes = [
        TreeChange(e.partition, e.key, None if e.encoded is None else e.encoded.hash)
        for e in elements
    ]
    store.prefetch_paths(head_root, [(c.partition, c.key) for c in tree_changes])
    root = tree_apply(store, head_root, tree_changes, TreeParams(domain.leaf_size))
    by_key = {(e.partition, e.key): e for e in elements}
    changeset = [
        _attributed(entry, by_key[(entry.partition, entry.key)], view, own_provenance)
        for entry in changeset_between(store, head_root, root)
    ]
    written = {entry.new_hash for entry in changeset}
    schema_hash = None
    if any(entry.partition == PARTITION_SCHEMA for entry in changeset):
        schema_hash = Tree.load(store, root).partitions[PARTITION_SCHEMA].root
    return _Prepared(
        root=root,
        changeset=changeset,
        bodies={key: None if e.encoded is None else e.encoded.body for key, e in by_key.items()},
        objects=[e.encoded for e in elements if e.encoded and e.encoded.hash in written],
        nodes=store.take_pending(),
        schema=schema,
        schema_hash=schema_hash,
        refresh_kinds=plan.refresh_kinds,
        provenance=plan.provenance,
        derived=list(report.derived_changes),
        vault_rows=vaulted.rows,
        vault_lineage=vaulted.lineage,
        vault_moved=vaulted.moved,
    )


def _schema_element(element: SchemaElementModel) -> _Element:
    return _Element(
        PARTITION_SCHEMA,
        element_key(element),
        encode_object(object_type_of(element), element.content()),
    )


def _data_element(change: DataChange, provenance: ProvenanceSpec) -> _Element:
    if isinstance(change, RecordUpsert):
        record = change.record
        encoded = encode_object(ObjectType.RECORD, record.content())
        return _Element(kind_partition(record.kind), record.key, encoded, provenance)
    if isinstance(change, RecordDelete):
        return _Element(kind_partition(change.kind), change.key, None, provenance)
    if isinstance(change, LinkUpsert):
        link = change.link
        encoded = encode_object(ObjectType.LINK, link.content())
        key = link_key(link.from_key, link.to_key)
        return _Element(relation_partition(link.relation), key, encoded, provenance)
    assert isinstance(change, LinkDelete)
    key = link_key(change.from_key, change.to_key)
    return _Element(relation_partition(change.relation), key, None, provenance)


def _attributed(
    entry: ChangesetEntry, element: _Element, view: MaterializedIndexView, own: str
) -> ChangesetEntry:
    """The changeset entry with its provenance map (changed Attribute -> entry id)."""
    spec = element.provenance

    def resolve(attribute: str) -> str:
        if isinstance(spec, str):
            return spec
        if spec is not None:
            return spec.get(attribute, own)
        return own

    attributes: list[str] = []
    parsed = parse_partition(entry.partition)
    if element.encoded is not None and parsed.type is not PartitionType.SCHEMA:
        new_values: Mapping[str, Any] = element.encoded.body.get("values", {})
        old_values: Mapping[str, Any] | None = None
        if entry.old_hash is not None:
            if parsed.type is PartitionType.KIND:
                record = view.record_state(parsed.element_id, entry.key)
                old_values = None if record is None else record.values
            else:
                from_key, to_key = split_link_key(entry.key)
                link = view.link_state(parsed.element_id, from_key, to_key)
                old_values = None if link is None else link.values
        attributes = changed_attributes(old_values, new_values)
    provenance = {a: resolve(a) for a in attributes} or {
        ELEMENT_PROVENANCE_KEY: resolve(ELEMENT_PROVENANCE_KEY)
    }
    return ChangesetEntry(entry.partition, entry.key, entry.old_hash, entry.new_hash, provenance)


def _materialized_changes(prepared: _Prepared) -> list[MaterializedChange]:
    return [
        MaterializedChange(entry, prepared.bodies[(entry.partition, entry.key)])
        for entry in prepared.changeset
    ]


async def _store_revision(
    session: AsyncSession,
    runtime: StoreRuntime,
    revision: Revision,
    prepared: _Prepared,
    provenance: Sequence[ProvenanceRow],
) -> RevisionRow:
    """Steps 3 to 5: objects, nodes, Revision, changeset and provenance."""
    settings = runtime.settings
    domain_id = revision.domain
    await PgObjectStore(
        session, domain_id, writes=settings.writes, batch=settings.nodes.prefetch_batch
    ).put_many(prepared.objects)
    await insert_rows(session, domain_id, prepared.vault_rows, at=revision.created_at)
    await insert_lineage(session, domain_id, prepared.vault_lineage, at=revision.created_at)
    await mark_moved(session, domain_id, prepared.vault_moved)
    await NodeRepository(
        session,
        domain_id,
        writes=settings.writes,
        cache=runtime.node_cache,
        batch=settings.nodes.prefetch_batch,
    ).put_many(prepared.nodes)
    stored = await RevisionRepository(session, domain_id).insert(revision)
    await ProvenanceRepository(session, domain_id, writes=settings.writes).insert_many(provenance)
    await ChangesetRepository(session, domain_id, writes=settings.writes).insert(
        stored.id, prepared.changeset
    )
    return stored


async def _record_events(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    revision: RevisionRow,
    changeset: Sequence[ChangesetEntry],
    message: str,
) -> None:
    """Step 8: audit entry and outbox event."""
    stats = changeset_stats(changeset)
    details: dict[str, Any] = {
        "branch": branch,
        "revision": hex(revision.id),
        "parents": [hex(p) for p in revision.parents],
        "changes": stats.changes,
        "partitions_changed": stats.partitions_changed,
        "message": message,
    }
    await AuditRepository(session).append(
        NewAuditEntry(
            domain_id=domain_id,
            at=revision.created_at,
            actor=revision.author,
            action=COMMIT_ACTION,
            target=f"{branch}@{hex(revision.id)}",
            details=details,
        )
    )
    await OutboxRepository(session).enqueue(
        domain_id, COMMITTED_EVENT, {"domain": domain_id, **details}
    )


async def _create_domain(
    session: AsyncSession,
    domain: Domain,
    author: Author,
    leaf_size: int,
    message: str,
    origin: Origin | None,
    runtime: StoreRuntime | None,
    clock: Clock,
) -> CommitResult:
    runtime = runtime or default_runtime()
    origin = origin or Origin(type=DOMAIN_ORIGIN, ref=domain.id)
    created_at = clock()
    params = TreeParams(leaf_size)
    await scope_to_domain(session, domain.id)
    await DomainRepository(session).create(
        DomainRow(
            id=domain.id,
            display_name=domain.display_name or domain.id,
            description=domain.description or None,
            default_branch=domain.default_branch,
            leaf_size=params.leaf_size,
            settings=dict(domain.settings),
            created_at=created_at,
            created_by=author.principal,
        )
    )
    element = _schema_element(domain)
    assert element.encoded is not None
    tree_change = TreeChange(element.partition, element.key, element.encoded.hash)
    settings = runtime.settings

    def build(sync: Session) -> tuple[bytes, dict[bytes, bytes], list[ChangesetEntry]]:
        store = PgNodeStore(sync, domain.id, cache=runtime.node_cache)
        root = tree_apply(store, None, [tree_change], params)
        entries = changeset_between(store, None, root)
        return root, store.take_pending(), entries

    root, nodes, entries = await session.run_sync(build)
    own = ProvenanceRow(
        id=new_id(IdPrefix.PROVENANCE),
        origin_type=origin.type,
        origin_ref=origin.model_dump(),
        actor=author.principal,
        at=created_at,
    )
    changeset = [
        ChangesetEntry(e.partition, e.key, e.old_hash, e.new_hash, {ELEMENT_PROVENANCE_KEY: own.id})
        for e in entries
    ]
    schema = Schema(domain=domain)
    prepared = _Prepared(
        root=root,
        changeset=changeset,
        bodies={(element.partition, element.key): element.encoded.body},
        objects=[element.encoded],
        nodes=nodes,
        schema=schema,
        schema_hash=_root_schema_hash(nodes, root),
    )
    revision = Revision(
        root=root,
        domain=domain.id,
        author=author,
        message=message,
        origin=origin,
        created_at=created_at,
        stats=changeset_stats(changeset),
    )
    stored = await _store_revision(session, runtime, revision, prepared, [own])
    await RefRepository(session, domain.id).create(
        domain.default_branch,
        stored.id,
        created_at=created_at,
        created_by=author.principal,
        kind=RefKind.BRANCH,
        materialization=Materialization.FULL,
        protected=True,
        reason=DOMAIN_ORIGIN,
    )
    await apply_changeset(
        session,
        domain.id,
        domain.default_branch,
        revision_id=stored.id,
        at=stored.created_at,
        changes=_materialized_changes(prepared),
        schema=schema,
        schema_hash=prepared.schema_hash,
        settings=settings,
        runtime=runtime,
    )
    await _record_events(session, domain.id, domain.default_branch, stored, changeset, message)
    return CommitResult(
        revision_id=stored.id,
        parents=(),
        root=root,
        generation=stored.generation,
        ref_version=1,
        created_at=stored.created_at,
        changeset=tuple(changeset),
    )


def _root_schema_hash(nodes: Mapping[bytes, bytes], root: bytes) -> bytes | None:
    """Schema partition hash of a tree whose root node is among `nodes`."""
    store = InMemoryNodeStore()
    store.put(root, nodes[root])
    ref = Tree.load(store, root).partitions.get(PARTITION_SCHEMA)
    return None if ref is None else ref.root
