"""Storage side of the embedding job (design 9.3): what to embed, and writing vectors.

Vectors live in `embeddings`, keyed by `(domain_id, object_hash, model, fingerprint)`, the
fingerprint being that of the Kind's embedding template (`KindTemplate.fingerprint`): every
branch and Revision holding the same Record content under the same template shares one vector,
so an overlay branch or a branch forked from `main` adds work only for the Records it changed,
while branches whose templates differ keep a vector each. Readers look a vector up with the
fingerprint of the template of the branch they read (`template_fingerprints`). `text_hash`
(SHA-256 of the embedded text) lets a pass skip Records whose text did not change and copy the
vector of an identical text, whatever its template, instead of calling the model.

`embedding_cursors` records, per branch and model, the head and the template fingerprints of
the last complete pass, so a pass over an unchanged branch reads no Record rows.

Reads of a branch scan its own `mat_records` rows only: an overlay's inherited rows belong to
its base branch, whose pass covers them. Everything here runs in the caller's transaction,
in the Domain's row-level security scope (`scope_to_domain`).
"""

from __future__ import annotations

import hashlib
import itertools
import time
from collections import OrderedDict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    LargeBinary,
    Table,
    Text,
    and_,
    bindparam,
    case,
    delete,
    literal,
    or_,
    select,
    tuple_,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Kind, Schema
from lorebase_core.model.embedding import TemplateError, kind_template
from lorebase_store.commit.schema import load_compiled
from lorebase_store.models import metadata
from lorebase_store.models.enums import Materialization, RefKind
from lorebase_store.storage import PRECISION_TTL_S, Precision, hnsw_index_name

EMBEDDINGS: Table = metadata.tables["embeddings"]
EMBEDDING_CURSORS: Table = metadata.tables["embedding_cursors"]
MAT_RECORDS: Table = metadata.tables["mat_records"]
MAT_SCHEMA: Table = metadata.tables["mat_schema"]
REFS: Table = metadata.tables["refs"]

_LOCK_SCOPE = b"lorebase.embeddings\x00"

_REUSE = sql_text(
    """
    INSERT INTO embeddings (domain_id, object_hash, model, fingerprint, dims, embedding,
                            text_hash)
    SELECT DISTINCT ON (t.object_hash, t.fingerprint) :domain, t.object_hash, e.model,
           t.fingerprint, e.dims, e.embedding, e.text_hash
    FROM unnest(CAST(:hashes AS bytea[]), CAST(:fingerprints AS text[]),
                CAST(:texts AS bytea[])) AS t(object_hash, fingerprint, text_hash)
    JOIN embeddings e ON e.domain_id = :domain AND e.model = :model AND e.dims = :dims
                     AND e.text_hash = t.text_hash
    ORDER BY t.object_hash, t.fingerprint
    ON CONFLICT (domain_id, object_hash, model, fingerprint) DO UPDATE
      SET dims = EXCLUDED.dims, embedding = EXCLUDED.embedding, text_hash = EXCLUDED.text_hash
    RETURNING object_hash, fingerprint
    """
).bindparams(
    bindparam("hashes", type_=ARRAY(LargeBinary())),
    bindparam("fingerprints", type_=ARRAY(Text())),
    bindparam("texts", type_=ARRAY(LargeBinary())),
)


def lock_key(domain_id: str) -> int:
    """Signed 64-bit advisory lock key of a Domain's embedding pass (one at a time)."""
    digest = hashlib.sha256(_LOCK_SCOPE + domain_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def text_hash(text: str) -> bytes:
    """SHA-256 of an embedded text."""
    return hashlib.sha256(text.encode("utf-8")).digest()


def template_fingerprints(kinds: Iterable[Kind]) -> dict[str, str]:
    """Fingerprint of the embedding template of each of `kinds` that has a usable one, by Kind
    id: the fingerprint its vectors are stored and looked up under."""
    found: dict[str, str] = {}
    for kind in kinds:
        try:
            template = kind_template(kind)
        except TemplateError:
            continue
        if template is not None:
            found[kind.id] = template.fingerprint
    return found


class SchemaCache[V]:
    """What was derived from a compiled schema (its embedding templates), by schema hash, for
    the `size` schemas used last: branches sharing a schema, and passes over an unchanged one,
    read and parse it once."""

    def __init__(self, size: int) -> None:
        if size < 1:
            raise ValueError("a schema cache holds at least one schema")
        self._size = size
        self._entries: OrderedDict[bytes, V] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, schema_hash: bytes) -> V | None:
        found = self._entries.get(schema_hash)
        if found is None:
            self.misses += 1
            return None
        self.hits += 1
        self._entries.move_to_end(schema_hash)
        return found

    def put(self, schema_hash: bytes, value: V) -> None:
        self._entries[schema_hash] = value
        self._entries.move_to_end(schema_hash)
        while len(self._entries) > self._size:
            self._entries.popitem(last=False)


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
    fingerprint: str
    """Fingerprint of the template of the row's Kind on the branch."""
    values: Mapping[str, Any]
    text_hash: bytes | None
    """Text hash of the stored vector, None when there is none (or it predates 0014)."""
    embedded: bool
    """A vector of the model exists for the row's object hash and template fingerprint."""


@dataclass(frozen=True, slots=True)
class NewVector:
    object_hash: bytes
    fingerprint: str
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
            # A branch still being built from its tree has no complete rows yet.
            REFS.c.materialization.not_in([Materialization.NONE, Materialization.BUILDING]),
            REFS.c.deleted_at.is_(None),
        )
        .order_by(REFS.c.name)
    )
    if names is not None:
        query = query.where(REFS.c.name.in_(list(names)))
    return [BranchHead(name, bytes(head)) for name, head in (await session.execute(query)).all()]


async def branch_schema_hash(session: AsyncSession, domain_id: str, branch: str) -> bytes | None:
    """Hash of the schema materialized at the branch head (the compiled document is not read)."""
    found = (
        await session.execute(
            select(MAT_SCHEMA.c.schema_hash).where(
                MAT_SCHEMA.c.domain_id == domain_id, MAT_SCHEMA.c.branch == branch
            )
        )
    ).scalar_one_or_none()
    return None if found is None else bytes(found)


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
    fingerprints: Mapping[str, str],
    rerender: Collection[str],
    after: tuple[str, str] | None,
    limit: int,
) -> list[Candidate]:
    """The branch's own live rows of the Kinds of `fingerprints` (Kind id to template
    fingerprint) in `(kind_id, key)` order after `after`: those without a vector of the model
    under their Kind's fingerprint, plus every row of the `rerender` Kinds (template changed or
    a reindex), with the text hash of their stored vector."""
    if not fingerprints:
        return []
    kinds = sorted(fingerprints)
    r = MAT_RECORDS.alias("r")
    e = EMBEDDINGS.alias("e")
    fingerprint = fingerprint_of(r.c.kind_id, fingerprints)
    missing = e.c.object_hash.is_(None)
    wanted = [k for k in rerender if k in fingerprints]
    query = (
        select(
            r.c.kind_id,
            r.c.key,
            r.c.object_hash,
            fingerprint.label("fingerprint"),
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
                    e.c.fingerprint == fingerprint,
                    e.c.dims == dims,
                ),
            )
        )
        .where(
            r.c.domain_id == domain_id,
            r.c.branch == branch,
            r.c.deleted.is_(False),
            r.c.kind_id.in_(kinds),
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
            fingerprint=str(row.fingerprint),
            values=row[4] or {},
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
    wanted: Sequence[tuple[bytes, str, bytes]],
) -> set[tuple[bytes, str]]:
    """Copy, for each `(object_hash, fingerprint, text_hash)`, a stored vector of the same text
    and model (whatever template rendered it); returns the `(object_hash, fingerprint)` pairs
    served this way."""
    if not wanted:
        return set()
    result = await session.execute(
        _REUSE,
        {
            "domain": domain_id,
            "model": model,
            "dims": dims,
            "hashes": [h for h, _, _ in wanted],
            "fingerprints": [f for _, f, _ in wanted],
            "texts": [t for _, _, t in wanted],
        },
    )
    return {(bytes(row[0]), str(row[1])) for row in result.all()}


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
                "fingerprint": v.fingerprint,
                "dims": len(v.vector),
                "embedding": list(v.vector),
                "text_hash": v.text_hash,
            }
            for v in vectors
        ]
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[e.c.domain_id, e.c.object_hash, e.c.model, e.c.fingerprint],
            set_={
                "dims": statement.excluded.dims,
                "embedding": statement.excluded.embedding,
                "text_hash": statement.excluded.text_hash,
            },
        )
    )


def fingerprint_of(kind_id: Any, fingerprints: Mapping[str, str]) -> Any:
    """SQL expression of the template fingerprint of a row's Kind (`kind_id`), from
    `fingerprints` (Kind id to fingerprint); NULL for other Kinds, which match no vector."""
    if not fingerprints:
        return literal(None, Text())
    return case(
        {kind: literal(f, Text()) for kind, f in sorted(fingerprints.items())}, value=kind_id
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


# Similar vectors --------------------------------------------------------------------------------

_SIMILAR = sql_text(
    """
    SELECT a.key AS key, n.key AS other, 1 - n.distance AS score
    FROM mat_records a
    JOIN embeddings ea ON ea.domain_id = a.domain_id AND ea.object_hash = a.object_hash
                      AND ea.model = :model AND ea.fingerprint = :fingerprint AND ea.dims = :dims
    CROSS JOIN LATERAL (
      SELECT b.key, eb.embedding <=> ea.embedding AS distance
      FROM mat_records b
      JOIN embeddings eb ON eb.domain_id = b.domain_id AND eb.object_hash = b.object_hash
                        AND eb.model = :model AND eb.fingerprint = :fingerprint
                        AND eb.dims = :dims
      WHERE b.domain_id = :domain AND b.branch = :branch AND b.kind_id = :kind
        AND b.deleted IS FALSE AND b.key <> a.key
      ORDER BY distance, b.key
      LIMIT :neighbours
    ) n
    WHERE a.domain_id = :domain AND a.branch = :branch AND a.kind_id = :kind
      AND a.deleted IS FALSE AND a.key = ANY(:keys) AND 1 - n.distance >= :threshold
    ORDER BY score DESC, a.key, n.key
    """
).bindparams(bindparam("keys", type_=ARRAY(Text())))


@dataclass(frozen=True, slots=True)
class SimilarPair:
    key: str
    other: str
    score: float


async def similar_vectors(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    kind_id: str,
    keys: Sequence[str],
    *,
    model: str,
    fingerprint: str,
    dims: int,
    threshold: float,
    neighbours: int,
) -> list[SimilarPair]:
    """For each of `keys` (live rows of `kind_id` on `branch`), its `neighbours` nearest Records
    of the same Kind by the cosine similarity of the vectors stored for `model` under the
    template `fingerprint`, kept when at least `threshold`; best first. Reads stored vectors
    only: no model is called. Records without a vector have no neighbour."""
    if not keys or neighbours < 1:
        return []
    rows = (
        await session.execute(
            _SIMILAR,
            {
                "domain": domain_id,
                "branch": branch,
                "kind": kind_id,
                "keys": list(keys),
                "model": model,
                "fingerprint": fingerprint,
                "dims": dims,
                "threshold": threshold,
                "neighbours": neighbours,
            },
        )
    ).all()
    return [SimilarPair(str(r.key), str(r.other), float(r.score)) for r in rows]


# Index precision --------------------------------------------------------------------------------

_INDEX_NAMES = sql_text(
    "SELECT c.relname FROM pg_index x JOIN pg_class c ON c.oid = x.indexrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = current_schema() AND c.relname IN (:full, :half) AND x.indisvalid "
    "ORDER BY c.oid DESC LIMIT 1"
)
_precision: dict[int, tuple[float, Precision]] = {}


async def index_precision(session: AsyncSession, dims: int) -> Precision:
    """`half` when the HNSW index of `dims` is on `halfvec` (storage profile, see
    `lorebase_store.storage`), else `full`: searches cast to the indexed type so the index
    serves them. With both valid (during a switch) the newest one; looked up again after
    `PRECISION_TTL_S`, which `storage apply` waits before it drops the older one."""
    now = time.monotonic()
    cached = _precision.get(dims)
    if cached is not None and now - cached[0] < PRECISION_TTL_S:
        return cached[1]
    full, half = hnsw_index_name(dims, "full"), hnsw_index_name(dims, "half")
    newest = (await session.execute(_INDEX_NAMES, {"full": full, "half": half})).scalar()
    precision: Precision = "half" if newest == half else "full"
    _precision[dims] = (now, precision)
    return precision


# Iterative index scans --------------------------------------------------------------------------

ITERATIVE_SCAN_MIN_VERSION = (0, 8)
"""First pgvector release with `hnsw.iterative_scan`."""

_EXTENSION_VERSION = sql_text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
_pgvector: tuple[int, ...] | None = None


def parse_version(text: str) -> tuple[int, ...]:
    """`0.8.0` -> `(0, 8, 0)`; a part that is not a plain number (`0-rc1`) ends the version."""
    parts: list[int] = []
    for part in text.strip().split("."):
        digits = "".join(itertools.takewhile(str.isdigit, part))
        if not digits:
            break
        parts.append(int(digits))
        if digits != part:
            break
    return tuple(parts)


async def pgvector_version(session: AsyncSession) -> tuple[int, ...] | None:
    """The installed pgvector version (`pg_extension.extversion`), read once per process;
    `None` when the extension is not installed (not remembered)."""
    global _pgvector
    if _pgvector is None:
        found = (await session.execute(_EXTENSION_VERSION)).scalar()
        if found is None:
            return None
        _pgvector = parse_version(str(found))
    return _pgvector


async def iterative_scans(session: AsyncSession) -> bool:
    """Whether the database's pgvector has iterative HNSW scans (0.8 or later)."""
    version = await pgvector_version(session)
    return version is not None and version >= ITERATIVE_SCAN_MIN_VERSION


async def use_iterative_scan(session: AsyncSession, max_scan_tuples: int) -> None:
    """Turn on relaxed-order iterative HNSW scans for the rest of the transaction
    (`SET LOCAL`), bounded to `max_scan_tuples` tuples per scan: the filters of a statement
    then no longer leave its index scan with fewer rows than asked for, short of the bound.
    Relaxed order is approximate: callers order the rows again by exact distance."""
    await session.execute(
        sql_text(
            "SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true), "
            "set_config('hnsw.max_scan_tuples', :tuples, true)"
        ),
        {"tuples": str(max_scan_tuples)},
    )
