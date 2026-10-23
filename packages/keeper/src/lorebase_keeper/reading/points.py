"""Where a read is served from: Domain, branch, Revision and the schema at that Revision.

Every read names a branch (default: the Domain's default branch) and may name `at`, a Revision
id or a tag. Without `at`, or when `at` is the branch head, the read hits the materialized
head of the branch. Otherwise it is served from the Revision tree (design 6.12), marked
`served_from: tree` in responses: key reads and listings ordered by key read the tree, and
reads that need materialized rows read a snapshot of the Revision (`reading.snapshots`).
Every step is authorized against what it reads (`services.reads`).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from fastapi import Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Schema
from lorebase_core.tree import PARTITION_SCHEMA
from lorebase_keeper.auth.audit import annotate
from lorebase_keeper.auth.deps import AuthDep, PrincipalDep
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.deps import KeeperSettingsDep
from lorebase_keeper.errors import NOT_FOUND, ProblemError
from lorebase_keeper.reading.caching import not_modified
from lorebase_keeper.reading.runtime import ReadRuntime
from lorebase_keeper.reading.sensitivity import UNCLEARED, Disclosure, Masking, masking_for
from lorebase_keeper.reading.visibility import Projection, projection, tells
from lorebase_keeper.services.reads import ReadGuard, ReadTarget, StoreReadSource
from lorebase_store.commit.schema import load_compiled
from lorebase_store.layout import BranchLayout
from lorebase_store.materialize.rebuild import load_schema
from lorebase_store.models.enums import Materialization, RefKind
from lorebase_store.nodestore import NodeRepository
from lorebase_store.reads import head_revision
from lorebase_store.repositories import (
    DomainRepository,
    DomainRow,
    MaterializedRepository,
    RefRepository,
    RefRow,
    RevisionRepository,
    RevisionRow,
    scope_to_domain,
)

REVISION_HEADER = "Lorebase-Revision"
"""Response header naming the Revision a read was served from (design 8.1)."""

_REVISION_ID = re.compile(r"^[0-9a-f]{64}$")


class ServedFrom(StrEnum):
    MATERIALIZED = "materialized"
    TREE = "tree"


@dataclass(frozen=True, slots=True)
class KindScope:
    """The Kinds a read may touch: every Kind (`kinds` is None), or only the Kinds named by
    the caller's Kind-narrowed grants.

    With `invisible` (`keeper.auth.authz.invisible_kinds`, design 13.3) the other Kinds do not
    exist for the caller: the read point's schema is the projection to `kinds`
    (`reading.visibility`), names are resolved against it and fail like unknown names, and
    nothing about those Kinds is refused with 403. Without it they are refused with 403
    naming the Kind.
    """

    guard: ReadGuard
    kinds: frozenset[str] | None = None
    invisible: bool = False

    def allows(self, kind: str) -> bool:
        return self.kinds is None or kind in self.kinds

    @property
    def variant(self) -> str:
        """Distinguishes cached responses built for callers limited to different Kinds."""
        if self.kinds is None:
            return ""
        mode = "invisible" if self.invisible else "kinds"
        return f"{mode}=" + ",".join(sorted(self.kinds))

    def visible(self, schema: Schema) -> Projection:
        """The schema as the caller sees it: the projection to its Kinds when they are
        invisible, else `schema` itself."""
        if self.kinds is None or not self.invisible:
            return Projection(schema, frozenset())
        return projection(schema, self.kinds)

    def precheck(self, kinds: Iterable[str]) -> None:
        """Kinds named by a request, before they are resolved: 403 for one the caller may
        not read, unless invisible Kinds are on (resolution against the visible schema then
        reports them like unknown names)."""
        if not self.invisible:
            self.require(kinds)

    def require(self, kinds: Iterable[str]) -> None:
        """Resolved Kinds: 403 naming the first Kind (by api name) the caller may not read;
        with invisible Kinds, 404 like an unknown Kind (resolution against the visible schema
        never yields one, this guards against a path that resolves elsewhere)."""
        for kind in sorted(kinds):
            if not self.allows(kind):
                if self.invisible:
                    raise not_found(f"Kind {kind}")
                raise self.guard.kind_denied(kind)


@dataclass(frozen=True, slots=True)
class ReadPoint:
    """A resolved read: the Domain, the branch, the Revision served, its schema (as the caller
    sees it: `KindScope.visible`), the Kinds
    the caller may read there and how sensitive values are masked for it (`masking`).
    `layout` says where the branch's materialized rows live (its own, or an overlay over a
    base branch); materialized reads take their rows from it. At another Revision, a read
    that needs materialized rows gets a point whose `layout` names a snapshot of the Revision
    (`snapshot`, `reading.snapshots`) and reads it like a head read."""

    domain: DomainRow
    branch: str
    revision: RevisionRow
    schema: Schema
    served_from: ServedFrom
    scope: KindScope
    layout: BranchLayout
    masking: Masking = UNCLEARED
    snapshot: bool = False
    hidden: frozenset[str] = frozenset()
    """Ids of the schema elements and Attributes the projection left out (`KindScope.visible`):
    what a candidate schema built on the visible one must not reuse."""
    hidden_names: frozenset[str] = frozenset()
    """Names of the schema elements the projection left out."""

    def tells(self, value: Any) -> bool:
        """Whether `value` (a Revision message, an origin document) names something the
        projection left out: such text is not shown (design 13.3)."""
        return tells(value, self.hidden, self.hidden_names)

    @property
    def variant(self) -> str:
        """What a cached response depends on besides the request: readable Kinds and
        clearance."""
        return "|".join(part for part in (self.scope.variant, self.masking.variant) if part)

    def disclosure(self, session: AsyncSession, runtime: ReadRuntime) -> Disclosure:
        """Resolves vault references in this read's responses."""
        return Disclosure(session, self.domain.id, (self.schema,), self.masking, runtime.vault)

    @property
    def materialized(self) -> bool:
        """Whether materialized rows serve the read: the branch head's, or a snapshot's."""
        return self.served_from is ServedFrom.MATERIALIZED or self.snapshot

    @property
    def revision_hex(self) -> str:
        return self.revision.id.hex()


def not_found(what: str) -> ProblemError:
    return ProblemError(NOT_FOUND, f"{what} not found.")


def read_guard() -> Callable[..., Awaitable[ReadGuard]]:
    """Dependency: the authenticated reader of the Domain named by the path (401 otherwise).

    `data.read` itself is checked once the read point is resolved (`services.reads`), and
    the Kinds a request touches against the point's `KindScope`.
    """

    async def dependency(
        request: Request, principal: PrincipalDep, auth: AuthDep, keeper: KeeperSettingsDep
    ) -> ReadGuard:
        domain_id = str(request.path_params["domain_id"])
        annotate(request, domain_id=domain_id, permission=Permission.DATA_READ.value)
        return ReadGuard(
            authorizer=auth.authorizer,
            principal=principal,
            permissions=(Permission.DATA_READ,),
            domain_id=domain_id,
            max_depth=keeper.reads.reachability_max_depth,
        )

    return dependency


async def open_domain(session: AsyncSession, guard: ReadGuard) -> DomainRow:
    """The Domain of the request, with the transaction scoped to it (403 or 404 when absent)."""
    await guard.precheck()
    await scope_to_domain(session, guard.domain_id)
    domain = await DomainRepository(session).get(guard.domain_id)
    if domain is None:
        raise guard.unknown(not_found(f"Domain {guard.domain_id}"), wide=await guard.wide())
    return domain


async def open_branch(
    session: AsyncSession, guard: ReadGuard, domain: DomainRow, branch: str | None
) -> RefRow:
    """The branch read (default: the Domain's default branch), authorized for the caller."""
    name = branch or domain.default_branch
    await guard.require_branch(name)
    ref = await RefRepository(session, domain.id).get(name)
    if ref is None or ref.kind is not RefKind.BRANCH:
        raise not_found(f"Branch {name}")
    return ref


async def find_revision(
    session: AsyncSession, domain_id: str, token: str
) -> tuple[RevisionRow, RefRow | None] | None:
    """The Revision named by an id, a tag or a branch (its head), and the ref used if any."""
    revisions = RevisionRepository(session, domain_id)
    if _REVISION_ID.match(token):
        found = await revisions.get(bytes.fromhex(token))
        if found is not None:
            return found, None
    named = await RefRepository(session, domain_id).get(token)
    if named is None or named.kind is RefKind.SNAPSHOT:
        return None
    return await revisions.require(named.head), named


async def open_revision(
    session: AsyncSession, guard: ReadGuard, domain: DomainRow, token: str
) -> RevisionRow:
    """The Revision named by `token`, authorized against what it is (see `services.reads`)."""
    wide = await guard.wide()
    found = await find_revision(session, domain.id, token)
    if found is None:
        raise guard.unknown(not_found(f"Revision {token}"), wide=wide)
    revision, named = found
    target = ReadTarget(
        revision=revision.id,
        ref=None if named is None else named.name,
        kind=None if named is None else named.kind,
    )
    await guard.require(StoreReadSource(session, domain), target, wide=wide)
    return revision


async def unchanged_head(
    session: AsyncSession,
    runtime: ReadRuntime,
    guard: ReadGuard,
    request: Request,
    *,
    branch: str | None,
) -> Response | None:
    """304 for a conditional GET at a branch head, decided from the head Revision alone.

    Runs only when the request carries `If-None-Match`. The Domain and `data.read` on the
    branch are authorized first; then `head_revision` (one primary-key read) gives the tag.
    Callers whose grants are narrowed to Kinds, unknown branches and tags that do not match
    go on to the full read, which answers them.
    """
    if "if-none-match" not in request.headers:
        return None
    domain = await open_domain(session, guard)
    name = branch or domain.default_branch
    try:
        await guard.require_branch(name)
    except ProblemError:
        return None
    head = await head_revision(session, domain.id, name)
    if head is None:
        return None
    # The tag of a full read depends on the caller's clearance and the default branch's tags
    # too (`ReadPoint.variant`): an older body must not be revalidated after a tag change.
    reference = await branch_schema(session, runtime, domain.id, domain.default_branch)
    masking = await masking_for(
        guard.authorizer, guard.principal, domain.id, reference, runtime.sensitivity
    )
    return not_modified(
        request,
        domain_id=domain.id,
        revision=head.hex(),
        headers={REVISION_HEADER: head.hex()},
        variant=masking.variant,
    )


async def resolve_point(
    session: AsyncSession,
    runtime: ReadRuntime,
    guard: ReadGuard,
    *,
    branch: str | None,
    at: str | None,
) -> ReadPoint:
    """Resolve and authorize the read point of a request (see module doc).

    Callers who may not read the point in full but hold grants narrowed to Kinds read it
    limited to those Kinds: such grants cover every branch and Revision.
    """
    domain = await open_domain(session, guard)
    try:
        ref, revision = await _open_point(session, guard, domain, branch, at)
        scope = KindScope(guard)
    except ProblemError as error:
        granted = await guard.granted_kinds() if error.status_code == 403 else frozenset()
        if not granted:
            raise
        ref, revision = await _locate_point(session, guard, domain, branch, at)
        scope = KindScope(guard, granted, invisible=guard.authorizer.invisible_kinds)
    layout = ref.layout
    reference = await branch_schema(session, runtime, domain.id, domain.default_branch)
    masking = await masking_for(
        guard.authorizer, guard.principal, domain.id, reference, runtime.sensitivity
    )
    # A branch still being built from its tree (`building`) has no complete rows yet: its
    # head is read like a past Revision.
    if revision.id == ref.head and ref.materialization is not Materialization.BUILDING:
        schema = await branch_schema(session, runtime, domain.id, ref.name)
        if schema is not None:
            seen = scope.visible(schema)
            return ReadPoint(
                domain,
                ref.name,
                revision,
                seen.schema,
                ServedFrom.MATERIALIZED,
                scope,
                layout,
                masking.over(schema),
                hidden=seen.hidden,
                hidden_names=seen.names,
            )
    schema = await schema_at(session, runtime, domain.id, revision)
    seen = scope.visible(schema)
    return ReadPoint(
        domain,
        ref.name,
        revision,
        seen.schema,
        ServedFrom.TREE,
        scope,
        layout,
        masking.over(schema),
        hidden=seen.hidden,
        hidden_names=seen.names,
    )


async def branch_schema(
    session: AsyncSession, runtime: ReadRuntime, domain_id: str, branch: str
) -> Schema | None:
    """Compiled schema of a branch head, cached by schema hash; None when not materialized."""
    materialized = await MaterializedRepository(
        session, domain_id, branch, writes=runtime.store_settings.writes
    ).schema()
    if materialized is None:
        return None
    schema = runtime.schemas.get(domain_id, materialized.schema_hash)
    if schema is None:
        schema = load_compiled(materialized.compiled)
        runtime.schemas.put(domain_id, materialized.schema_hash, schema)
    return schema


async def _open_point(
    session: AsyncSession, guard: ReadGuard, domain: DomainRow, branch: str | None, at: str | None
) -> tuple[RefRow, RevisionRow]:
    """Branch and Revision of a read, authorized in full."""
    ref = await open_branch(session, guard, domain, branch)
    if at is None:
        return ref, await RevisionRepository(session, domain.id).require(ref.head)
    return ref, await open_revision(session, guard, domain, at)


async def _locate_point(
    session: AsyncSession, guard: ReadGuard, domain: DomainRow, branch: str | None, at: str | None
) -> tuple[RefRow, RevisionRow]:
    """Branch and Revision of a read limited to Kinds; unknown names answer 403."""
    ref = await RefRepository(session, domain.id).get(branch or domain.default_branch)
    if ref is None or ref.kind is not RefKind.BRANCH:
        raise guard.denied()
    if at is None:
        return ref, await RevisionRepository(session, domain.id).require(ref.head)
    found = await find_revision(session, domain.id, at)
    if found is None:
        raise guard.denied()
    return ref, found[0]


async def schema_at(
    session: AsyncSession, runtime: ReadRuntime, domain_id: str, revision: RevisionRow
) -> Schema:
    """Schema of a Revision, read from its tree and cached by schema partition hash."""
    settings = runtime.store_settings
    nodes = NodeRepository(
        session,
        domain_id,
        writes=settings.writes,
        cache=runtime.store.node_cache,
        batch=settings.nodes.prefetch_batch,
    )
    tree = await nodes.load_tree(revision.root)
    partition = tree.partitions.get(PARTITION_SCHEMA)
    if partition is not None:
        cached = runtime.schemas.get(domain_id, partition.root)
        if cached is not None:
            return cached
    schema, schema_hash = await load_schema(session, domain_id, tree, runtime=runtime.store)
    runtime.schemas.put(domain_id, schema_hash, schema)
    return schema
