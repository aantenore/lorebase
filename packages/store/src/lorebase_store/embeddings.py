"""Storage side of the embedding job (design 9.3): what to embed, and writing vectors.

Vectors live in `embeddings`, keyed by `(domain_id, object_hash, model)`: every branch and
Revision holding the same Record content shares one vector, so an overlay branch or a branch
forked from `main` adds work only for the Records it changed. `text_hash` (SHA-256 of the
embedded text) lets a pass skip Records whose text did not change and copy the vector of an
identical text instead of calling the model.

`embedding_cursors` records, per branch and model, the head and the template fingerprints of
the last complete pass, so a pass over an unchanged branch reads no Record rows.

Reads of a branch scan its own `mat_records` rows only: an overlay's inherited rows belong to
its base branch, whose pass covers them. Everything here runs in the caller's transaction,
in the Domain's row-level security scope (`scope_to_domain`).
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    LargeBinary,
    Table,
    and_,
    bindparam,
    delete,
    literal,
    or_,
    select,
    tuple_,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Schema
from lorebase_store.commit.schema import load_compiled
from lorebase_store.models import metadata
from lorebase_store.models.enums import Materialization, RefKind

EMBEDDINGS: Table = metadata.tables["embeddings"]
EMBEDDING_CURSORS: Table = metadata.tables["embedding_cursors"]
MAT_RECORDS: Table = metadata.tables["mat_records"]
MAT_SCHEMA: Table = metadata.tables["mat_schema"]
REFS: Table = metadata.tables["refs"]

_LOCK_SCOPE = b"lorebase.embeddings\x00"

_REUSE = sql_text(
    """
    INSERT INTO embeddings (domain_id, object_hash, model, dims, embedding, text_hash)
    SELECT DISTINCT ON (t.object_hash) :domain, t.object_hash, e.model, e.dims, e.embedding,
           e.text_hash
    FROM unnest(CAST(:hashes AS bytea[]), CAST(:texts AS bytea[])) AS t(object_hash, text_hash)
    JOIN embeddings e ON e.domain_id = :domain AND e.model = :model AND e.dims = :dims
                     AND e.text_hash = t.text_hash
    ORDER BY t.object_hash
    ON CONFLICT (domain_id, object_hash, model) DO UPDATE
      SET dims = EXCLUDED.dims, embedding = EXCLUDED.embedding, text_hash = EXCLUDED.text_hash
    RETURNING object_hash
    """
).bindparams(
    bindparam("hashes", type_=ARRAY(LargeBinary())),
    bindparam("texts", type_=ARRAY(LargeBinary())),
)


def lock_key(domain_id: str) -> int:
    """Signed 64-bit advisory lock key of a Domain's embedding pass (one at a time)."""
    digest = hashlib.sha256(_LOCK_SCOPE + domain_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def text_hash(text: str) -> bytes:
    """SHA-256 of an embedded text."""
    return hashlib.sha256(text.encode("utf-8")).digest()


@dataclass(frozen=True, slots=True)
class BranchHead:
    name: str
    head: bytes


@dataclass(frozen=True, slots=True)
class Cursor:
    head: bytes
    fingerprints: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class Candidate:
    """A Record row that may need a vector."""

    kind_id: str
    key: str
    object_hash: bytes
    values: Mapping[str, Any]
    text_hash: bytes | None
    """Text hash of the stored vector, None when there is none (or it predates 0014)."""
    embedded: bool
    """A vector of the model exists for the row's object hash."""


@dataclass(frozen=True, slots=True)
class NewVector:
    object_hash: bytes
    text_hash: bytes
    vector: Sequence[float]


async def materialized_branches(
    session: AsyncSession, domain_id: str, names: Collection[str] | None = None
) -> list[BranchHead]:
    """Materialized branches of the Domain (all, or those of `names`), by name."""
    query = (
        select(REFS.c.name, REFS.c.head)
        .where(
            REFS.c.domain_id == domain_id,
            REFS.c.kind == RefKind.BRANCH,
            REFS.c.materialization != Materialization.NONE,
            REFS.c.deleted_at.is_(None),
        )
        .order_by(REFS.c.name)
    )
    if names is not None:
        query = query.where(REFS.c.name.in_(list(names)))
    return [BranchHead(name, bytes(head)) for name, head in (await session.execute(query)).all()]


async def branch_schema(session: AsyncSession, domain_id: str, branch: str) -> Schema | None:
    """The compiled schema materialized at the branch head."""
    compiled = (
        await session.execute(
            select(MAT_SCHEMA.c.compiled).where(
                MAT_SCHEMA.c.domain_id == domain_id, MAT_SCHEMA.c.branch == branch
            )
        )
    ).scalar_one_or_none()
    return None if compiled is None else load_compiled(compiled)


async def read_cursor(
    session: AsyncSession, domain_id: str, branch: str, model: str
) -> Cursor | None:
    c = EMBEDDING_CURSORS
    found = (
        await session.execute(
            select(c.c.head, c.c.fingerprints).where(
                c.c.domain_id == domain_id, c.c.branch == branch, c.c.model == model
            )
        )
    ).one_or_none()
    if found is None:
        return None
    return Cursor(bytes(found[0]), {str(k): str(v) for k, v in dict(found[1]).items()})


async def save_cursor(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    model: str,
    cursor: Cursor,
    *,
    at: datetime,
) -> None:
    """Record a complete pass. A branch deleted meanwhile has no cursor to keep: nothing is
    written (the row would violate the reference to `refs`)."""
    c = EMBEDDING_CURSORS
    exists = (
        await session.execute(
            select(literal(1)).where(REFS.c.domain_id == domain_id, REFS.c.name == branch)
        )
    ).scalar_one_or_none()
    if exists is None:
        return
    values = {"head": cursor.head, "fingerprints": dict(cursor.fingerprints), "updated_at": at}
    await session.execute(
        insert(c)
        .values(domain_id=domain_id, branch=branch, model=model, **values)
        .on_conflict_do_update(index_elements=[c.c.domain_id, c.c.branch, c.c.model], set_=values)
    )


async def candidates(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    *,
    model: str,
    dims: int,
    kinds: Collection[str],
    rerender: Collection[str],
    after: tuple[str, str] | None,
    limit: int,
) -> list[Candidate]:
    """The branch's own live rows of `kinds` in `(kind_id, key)` order after `after`: those
    without a vector of the model, plus every row of the `rerender` Kinds (template changed
    or a reindex), with the text hash of their stored vector."""
    if not kinds:
        return []
    r = MAT_RECORDS.alias("r")
    e = EMBEDDINGS.alias("e")
    missing = e.c.object_hash.is_(None)
    wanted = [k for k in rerender if k in kinds]
    query = (
        select(
            r.c.kind_id,
            r.c.key,
            r.c.object_hash,
            r.c["values"],
            e.c.text_hash,
            e.c.object_hash.is_not(None).label("embedded"),
        )
        .select_from(
            r.outerjoin(
                e,
                and_(
                    e.c.domain_id == r.c.domain_id,
                    e.c.object_hash == r.c.object_hash,
                    e.c.model == model,
                    e.c.dims == dims,
                ),
            )
        )
        .where(
            r.c.domain_id == domain_id,
            r.c.branch == branch,
            r.c.deleted.is_(False),
            r.c.kind_id.in_(sorted(kinds)),
            or_(missing, r.c.kind_id.in_(wanted)) if wanted else missing,
        )
        .order_by(r.c.kind_id, r.c.key)
        .limit(limit)
    )
    if after is not None:
        query = query.where(
            tuple_(r.c.kind_id, r.c.key) > tuple_(literal(after[0]), literal(after[1]))
        )
    rows = (await session.execute(query)).all()
    return [
        Candidate(
            kind_id=row.kind_id,
            key=row.key,
            object_hash=bytes(row.object_hash),
            values=row[3] or {},
            text_hash=None if row.text_hash is None else bytes(row.text_hash),
            embedded=bool(row.embedded),
        )
        for row in rows
    ]


async def reuse_vectors(
    session: AsyncSession,
    domain_id: str,
    *,
    model: str,
    dims: int,
    wanted: Sequence[tuple[bytes, bytes]],
) -> set[bytes]:
    """Copy, for each `(object_hash, text_hash)`, a stored vector of the same text and model;
    returns the object hashes served this way."""
    if not wanted:
        return set()
    result = await session.execute(
        _REUSE,
        {
            "domain": domain_id,
            "model": model,
            "dims": dims,
            "hashes": [h for h, _ in wanted],
            "texts": [t for _, t in wanted],
        },
    )
    return {bytes(row[0]) for row in result.all()}


async def store_vectors(
    session: AsyncSession, domain_id: str, *, model: str, vectors: Sequence[NewVector]
) -> None:
    """Insert or replace the vectors of the model."""
    if not vectors:
        return
    e = EMBEDDINGS
    statement = insert(e).values(
        [
            {
                "domain_id": domain_id,
                "object_hash": v.object_hash,
                "model": model,
                "dims": len(v.vector),
                "embedding": list(v.vector),
                "text_hash": v.text_hash,
            }
            for v in vectors
        ]
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[e.c.domain_id, e.c.object_hash, e.c.model],
            set_={
                "dims": statement.excluded.dims,
                "embedding": statement.excluded.embedding,
                "text_hash": statement.excluded.text_hash,
            },
        )
    )


async def prune_models(session: AsyncSession, domain_id: str, *, keep: str) -> int:
    """Delete the vectors and cursors of every model but `keep`; returns the vectors deleted."""
    await session.execute(
        delete(EMBEDDING_CURSORS).where(
            EMBEDDING_CURSORS.c.domain_id == domain_id, EMBEDDING_CURSORS.c.model != keep
        )
    )
    result = await session.execute(
        delete(EMBEDDINGS).where(EMBEDDINGS.c.domain_id == domain_id, EMBEDDINGS.c.model != keep)
    )
    return int(getattr(result, "rowcount", 0) or 0)
