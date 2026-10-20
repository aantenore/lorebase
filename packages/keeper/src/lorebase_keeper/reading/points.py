"""Where a read is served from: Domain, branch, Revision and the schema at that Revision.

Every read names a branch (default: the Domain's default branch) and may name `at`, a Revision
id or a tag. Without `at`, or when `at` is the branch head, the read hits the materialized
head of the branch. Otherwise it is served from the Revision tree (design 6.12): key reads and
Kind listings only, slower, and marked `served_from: tree` in responses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Schema
from lorebase_core.tree import PARTITION_SCHEMA
from lorebase_keeper.errors import NOT_FOUND, ProblemError
from lorebase_keeper.reading.runtime import ReadRuntime
from lorebase_store.commit.schema import load_compiled
from lorebase_store.materialize.rebuild import load_schema
from lorebase_store.models.enums import RefKind
from lorebase_store.nodestore import NodeRepository
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
class ReadPoint:
    """A resolved read: the Domain, the branch, the Revision served and its schema."""

    domain: DomainRow
    branch: str
    revision: RevisionRow
    schema: Schema
    served_from: ServedFrom

    @property
    def materialized(self) -> bool:
        return self.served_from is ServedFrom.MATERIALIZED

    @property
    def revision_hex(self) -> str:
        return self.revision.id.hex()


def not_found(what: str) -> ProblemError:
    return ProblemError(NOT_FOUND, f"{what} not found.")


async def require_domain(session: AsyncSession, domain_id: str) -> DomainRow:
    """The Domain, with the transaction scoped to it; 404 when it does not exist."""
    await scope_to_domain(session, domain_id)
    domain = await DomainRepository(session).get(domain_id)
    if domain is None:
        raise not_found(f"Domain {domain_id}")
    return domain


async def resolve_revision(session: AsyncSession, domain_id: str, ref: str) -> RevisionRow:
    """A Revision named by its id, a tag or a branch (its head); 404 when unknown."""
    revisions = RevisionRepository(session, domain_id)
    if _REVISION_ID.match(ref):
        found = await revisions.get(bytes.fromhex(ref))
        if found is not None:
            return found
    named = await RefRepository(session, domain_id).get(ref)
    if named is None:
        raise not_found(f"Revision {ref}")
    return await revisions.require(named.head)


async def require_branch(session: AsyncSession, domain: DomainRow, branch: str | None) -> RefRow:
    """The branch (default: the Domain's default branch); 404 for unknown names and tags."""
    name = branch or domain.default_branch
    ref = await RefRepository(session, domain.id).get(name)
    if ref is None or ref.kind is not RefKind.BRANCH:
        raise not_found(f"Branch {name}")
    return ref


async def resolve_point(
    session: AsyncSession,
    runtime: ReadRuntime,
    domain_id: str,
    *,
    branch: str | None,
    at: str | None,
) -> ReadPoint:
    """Resolve the read point of a request (see module doc)."""
    domain = await require_domain(session, domain_id)
    ref = await require_branch(session, domain, branch)
    revisions = RevisionRepository(session, domain.id)
    revision = (
        await revisions.require(ref.head)
        if at is None
        else await resolve_revision(session, domain.id, at)
    )
    if revision.id == ref.head:
        materialized = await MaterializedRepository(
            session, domain.id, ref.name, writes=runtime.store_settings.writes
        ).schema()
        if materialized is not None:
            schema = runtime.schemas.get(domain.id, materialized.schema_hash)
            if schema is None:
                schema = load_compiled(materialized.compiled)
                runtime.schemas.put(domain.id, materialized.schema_hash, schema)
            return ReadPoint(domain, ref.name, revision, schema, ServedFrom.MATERIALIZED)
    schema = await schema_at(session, runtime, domain.id, revision)
    return ReadPoint(domain, ref.name, revision, schema, ServedFrom.TREE)


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
