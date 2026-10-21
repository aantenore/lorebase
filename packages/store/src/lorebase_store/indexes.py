"""Per-Attribute partial indexes on `mat_records` (design 7.2, store schema notes).

Attributes flagged `indexed` or `unique`, and `ref` Attributes that back a foreign-key
Relation, get one expression index per Domain, branch, Kind and Attribute. The index lives on
the hash partition of `mat_records` that holds the Domain, so building it scans one partition
and never locks the others:

    CREATE [UNIQUE] INDEX mat_records_attr_<digest> ON mat_records_p<n>
        (<expression>[, key]) WHERE domain_id = '<d>' AND branch = '<b>' AND kind_id = '<k>'

The predicate pins Domain, branch and Kind, so branches with different schemas never share an
index and a unique index is unique per branch. A query uses the index only when it carries the
same predicate literals and the same expression; `IndexSpec.expression` and `predicate_sql`
are the single source of both (see `lorebase_store.reads.query_by_attribute`).

Each index carries a JSON comment naming its Domain, branch, Kind and Attribute, which is how
existing indexes are found again; names start with `INDEX_PREFIX`, which schema comparison
ignores.

When a schema change lands, `BranchIndexes.retire` runs before the materialized rows are
written and drops, inside the transaction, the indexes that would reject the new rows (unique
ones, and casts of an Attribute whose type changed); other stale indexes are dropped later by
a queued job. `BranchIndexes.ensure` runs after the rows are written and creates the missing
indexes inside the transaction when the Kind is small (`store.indexes.inline_max_rows`),
otherwise it queues a job that `run_index_jobs` builds with `CREATE INDEX CONCURRENTLY`.

An overlay branch (`lorebase_store.layout`) gets the same indexes on its own rows, none unique
(`plan_indexes(..., overlay=True)`), when it is created or re-forked; they are always queued,
since even an index on few rows is built by scanning the whole partition. Until they are
built, the overlay half of a read scans the overlay's rows of the Kind by primary key.

Index DDL needs the role that owns `mat_records`. For a session role that does not own it (the
least-privileged application role subject to row-level security):

- with a maintenance role configured (`database.maintenance_dsn`, `StoreRuntime.
  index_maintenance`), every change is queued as a job, and the job worker
  (`python -m lorebase_store.jobs work`) runs `run_index_job` with the maintenance engine.
  Indexes that would reject the new rows cannot be dropped before they are written: their
  drops are queued, forced, in a transaction of their own (a warning names them), so a commit
  that fails on them succeeds when retried after the worker has run (if it is never retried,
  the next schema change or `pending_index_plan` brings the index back);
- otherwise index management is skipped, as with `store.indexes.enabled: false`, with a
  warning once per process; `pending_index_plan` (`python -m lorebase_store.indexes plan
  --domain <id>`) prints the DDL an administrator applies.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lorebase_core.model import Attribute, Kind, RelationBacking, Schema
from lorebase_core.types import AttributeType
from lorebase_store.jobs import INDEX_JOB, JobRow
from lorebase_store.layout import load_layout
from lorebase_store.models.enums import JobStatus
from lorebase_store.repositories.scope import scope_to_domain
from lorebase_store.settings import IndexSettings

log = logging.getLogger(__name__)

INDEX_PREFIX = "mat_records_attr_"
"""Prefix of runtime-managed per-Attribute indexes (store schema notes)."""

INDEX_JOB_TYPE = INDEX_JOB
"""`jobs.type` of queued index builds and drops."""

PARENT_TABLE = "mat_records"
_NAME_DIGEST_CHARS = 24
_COMMENT_VERSION = 1
_NAME_SCOPE = b"lb1\x00mat-index\x00"
_LOCK_SCOPE = b"lb1\x00mat-index-ddl\x00"
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

_CASTS: Mapping[AttributeType, str] = {
    AttributeType.INTEGER: "bigint",
    AttributeType.DECIMAL: "numeric",
    AttributeType.FLOAT: "double precision",
    AttributeType.BOOLEAN: "boolean",
}
"""Types compared as SQL values; canonical text of the others already orders correctly or
is compared for equality only."""

_TEXT_TYPES = frozenset(
    {
        AttributeType.STRING,
        AttributeType.URI,
        AttributeType.ENUM,
        AttributeType.REF,
        AttributeType.DATE,
        AttributeType.TIMESTAMP,
        AttributeType.DURATION,
    }
)
"""Types indexed as their canonical text. Dates and timestamps are fixed-width ISO text, so
text order is time order; durations support equality only."""


class IndexMethod(StrEnum):
    BTREE = "btree"
    HASH = "hash"


class IndexReason(StrEnum):
    """Why an Attribute is indexed."""

    INDEXED = "indexed"
    UNIQUE = "unique"
    FOREIGN_KEY = "foreign_key"


@dataclass(frozen=True, slots=True)
class IndexSpec:
    """The index one Attribute of one Kind needs, independent of Domain and branch.

    `expression` is SQL over the `values` column; `cast` is the SQL type a compared value
    must be cast to (`None` for text). Non-unique btree indexes end with `key`, so equality
    lookups come back in key order and range scans page by `(value, key)`.
    """

    kind_id: str
    attribute_id: str
    attribute_type: AttributeType
    method: IndexMethod
    unique: bool
    expression: str
    cast: str | None
    reasons: tuple[IndexReason, ...]

    @property
    def columns(self) -> str:
        if self.method is IndexMethod.BTREE and not self.unique:
            return f"{self.expression}, key"
        return self.expression

    @property
    def ordered(self) -> bool:
        """Whether range comparisons and ordering can use the index."""
        return self.method is IndexMethod.BTREE and self.attribute_type is not AttributeType.TEXT

    @property
    def digest(self) -> bool:
        """Whether the index holds a digest of the value (unique long text)."""
        return self.expression.startswith("md5(")

    @property
    def value_type(self) -> str:
        """How values compare: `md5` (digest), the cast type, or `text`; the `p_type`
        argument of `lb_query_by_attribute`."""
        if self.digest:
            return "md5"
        return self.cast or "text"

    def value_sql(self, placeholder: str) -> str:
        """SQL of a value compared with `expression`; `placeholder` binds its canonical text."""
        value = f"CAST({placeholder} AS text)"
        if self.digest:
            return f"md5({value})"
        if self.cast is not None:
            return f"CAST({value} AS {self.cast})"
        return value

    def predicate_sql(self, domain_id: str, branch: str) -> str:
        """The index predicate for one Domain and branch (see `predicate_sql`)."""
        return predicate_sql(domain_id, branch, self.kind_id)

    def name(self, domain_id: str, branch: str) -> str:
        digest = hashlib.sha256(
            _NAME_SCOPE
            + "\x00".join(
                (
                    domain_id,
                    branch,
                    self.kind_id,
                    self.attribute_id,
                    self.method.value,
                    "unique" if self.unique else "plain",
                    self.expression,
                )
            ).encode("utf-8")
        ).hexdigest()
        return INDEX_PREFIX + digest[:_NAME_DIGEST_CHARS]


@dataclass(frozen=True, slots=True)
class SkippedAttribute:
    """An Attribute that asks for an index but whose type has no useful expression index."""

    kind_id: str
    attribute_id: str
    attribute_type: AttributeType


@dataclass(frozen=True, slots=True)
class IndexPlan:
    indexes: tuple[IndexSpec, ...]
    skipped: tuple[SkippedAttribute, ...] = ()

    def by_attribute(self) -> dict[tuple[str, str], IndexSpec]:
        return {(spec.kind_id, spec.attribute_id): spec for spec in self.indexes}


@dataclass(frozen=True, slots=True)
class ExistingIndex:
    """A per-Attribute index found in the catalog."""

    name: str
    partition: str
    branch: str
    kind_id: str
    attribute_id: str
    unique: bool
    cast: str | None
    valid: bool


@dataclass
class IndexSync:
    """What one synchronization did: index names created, dropped and queued."""

    created: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    queued: list[str] = field(default_factory=list)


# Planning (pure) --------------------------------------------------------------------------------


def values_text(attribute_id: str) -> str:
    """SQL text of one Attribute value: `("values" ->> '<attribute id>')`."""
    return f'("values" ->> {sql_literal(_checked_id(attribute_id))})'


def attribute_expression(attribute: Attribute, *, unique: bool) -> tuple[str, str | None] | None:
    """`(expression, cast)` indexing `attribute`, or `None` when its type is not indexable."""
    name = attribute.type.name
    raw = values_text(attribute.id)
    if name in _CASTS:
        cast = _CASTS[name]
        return f"({raw}::{cast})", cast
    if name in _TEXT_TYPES:
        return raw, None
    if name is AttributeType.TEXT:
        # Long text exceeds btree tuple limits: equality by hash, uniqueness by digest.
        return (f"md5{raw}", None) if unique else (raw, None)
    return None


def plan_indexes(schema: Schema, *, overlay: bool = False) -> IndexPlan:
    """Indexes the materialized head of a branch with `schema` needs; pure and deterministic.

    With `overlay`, those of an overlay branch's own rows: the same expressions and
    predicates, so the overlay half of its reads uses them as the base half uses the base's,
    but none unique. Uniqueness on an overlay holds for its resolved rows, which the commit
    checks; its own rows alone include tombstones, which keep their values.
    """
    foreign_keys = {
        (relation.from_kind, relation.foreign_key_attribute)
        for relation in schema.relations.values()
        if relation.backing is RelationBacking.FOREIGN_KEY
        and relation.foreign_key_attribute is not None
    }
    indexes: list[IndexSpec] = []
    skipped: list[SkippedAttribute] = []
    for kind in sorted(schema.kinds.values(), key=lambda k: k.id):
        for attribute in sorted(kind.attributes.values(), key=lambda a: a.id):
            reasons = _reasons(kind, attribute, foreign_keys)
            if not reasons:
                continue
            spec = _spec(kind, attribute, reasons)
            if spec is None:
                skipped.append(SkippedAttribute(kind.id, attribute.id, attribute.type.name))
            else:
                indexes.append(replace(spec, unique=False) if overlay else spec)
    return IndexPlan(tuple(indexes), tuple(skipped))


def _reasons(
    kind: Kind, attribute: Attribute, foreign_keys: set[tuple[str, str]]
) -> tuple[IndexReason, ...]:
    reasons: list[IndexReason] = []
    if attribute.indexed:
        reasons.append(IndexReason.INDEXED)
    if attribute.unique:
        reasons.append(IndexReason.UNIQUE)
    if (kind.id, attribute.id) in foreign_keys:
        reasons.append(IndexReason.FOREIGN_KEY)
    return tuple(reasons)


def _spec(kind: Kind, attribute: Attribute, reasons: tuple[IndexReason, ...]) -> IndexSpec | None:
    unique = IndexReason.UNIQUE in reasons
    found = attribute_expression(attribute, unique=unique)
    if found is None:
        return None
    expression, cast = found
    hashed = attribute.type.name is AttributeType.TEXT and not unique
    return IndexSpec(
        kind_id=kind.id,
        attribute_id=attribute.id,
        attribute_type=attribute.type.name,
        method=IndexMethod.HASH if hashed else IndexMethod.BTREE,
        unique=unique,
        expression=expression,
        cast=cast,
        reasons=reasons,
    )


# SQL text ---------------------------------------------------------------------------------------


def sql_literal(value: str) -> str:
    """A standard SQL string literal (quotes doubled; NUL rejected).

    Backslashes are left as they are, which is safe only with `standard_conforming_strings`
    on: the Postgres default, and pinned by `db.create_engine` on every connection.
    """
    if "\x00" in value:
        raise ValueError("SQL literals cannot contain NUL")
    return "'" + value.replace("'", "''") + "'"


def _checked_id(element_id: str) -> str:
    if not re.fullmatch(r"[a-z]{3}_[0-9A-Z]{26}", element_id):
        raise ValueError(f"not a schema element id: {element_id!r}")
    return element_id


def _checked_identifier(name: str) -> str:
    if not _IDENTIFIER.match(name):
        raise ValueError(f"unexpected SQL identifier {name!r}")
    return name


def predicate_sql(domain_id: str, branch: str, kind_id: str) -> str:
    """Partial index predicate; queries repeat it literally to match the index."""
    return (
        f"domain_id = {sql_literal(domain_id)} AND branch = {sql_literal(branch)} "
        f"AND kind_id = {sql_literal(_checked_id(kind_id))}"
    )


def create_index_sql(
    spec: IndexSpec, domain_id: str, branch: str, partition: str, *, concurrently: bool
) -> str:
    return (
        f"CREATE {'UNIQUE ' if spec.unique else ''}INDEX "
        f"{'CONCURRENTLY ' if concurrently else ''}IF NOT EXISTS {spec.name(domain_id, branch)} "
        f"ON {_checked_identifier(partition)} USING {spec.method.value} ({spec.columns}) "
        f"WHERE {predicate_sql(domain_id, branch, spec.kind_id)}"
    )


def comment_sql(spec: IndexSpec, domain_id: str, branch: str) -> str:
    meta = {
        "v": _COMMENT_VERSION,
        "domain": domain_id,
        "branch": branch,
        "kind": spec.kind_id,
        "attribute": spec.attribute_id,
        "unique": spec.unique,
        "cast": spec.cast,
    }
    payload = json.dumps(meta, sort_keys=True, separators=(",", ":"))
    return f"COMMENT ON INDEX {spec.name(domain_id, branch)} IS {sql_literal(payload)}"


def drop_index_sql(name: str, *, concurrently: bool) -> str:
    mode = "CONCURRENTLY " if concurrently else ""
    return f"DROP INDEX {mode}IF EXISTS {_checked_identifier(name)}"


# Catalog ----------------------------------------------------------------------------------------

_PARTITION_OF = text(
    rf"""
    SELECT c.relname
    FROM pg_inherits i
    JOIN pg_class c ON c.oid = i.inhrelid
    WHERE i.inhparent = CAST('{PARENT_TABLE}' AS regclass)
      AND satisfies_hash_partition(
            CAST('{PARENT_TABLE}' AS regclass),
            CAST((regexp_match(pg_get_expr(c.relpartbound, c.oid), 'modulus (\d+)', 'i'))[1]
                 AS int),
            CAST((regexp_match(pg_get_expr(c.relpartbound, c.oid), 'remainder (\d+)', 'i'))[1]
                 AS int),
            CAST(:domain AS text))
    """
)

_EXISTING = text(
    rf"""
    SELECT c.relname AS name, t.relname AS partition, x.indisvalid AS valid,
           d.description AS meta
    FROM pg_class c
    JOIN pg_index x ON x.indexrelid = c.oid
    JOIN pg_class t ON t.oid = x.indrelid
    JOIN pg_description d
      ON d.objoid = c.oid AND d.classoid = CAST('pg_class' AS regclass) AND d.objsubid = 0
    WHERE c.relkind = 'i'
      AND c.relname LIKE '{INDEX_PREFIX.replace("_", r"\_")}%'
      AND c.relnamespace = (SELECT relnamespace FROM pg_class
                            WHERE oid = CAST('{PARENT_TABLE}' AS regclass))
      AND d.description LIKE '{{%'
      AND CAST(d.description AS jsonb) ->> 'domain' = :domain
    ORDER BY c.relname
    """
)

_partitions: dict[str, str] = {}
"""Domain id -> partition of `mat_records`; partitions are fixed once the schema exists."""


async def partition_of(session: AsyncSession, domain_id: str) -> str:
    """Name of the `mat_records` hash partition that holds `domain_id`."""
    found = _partitions.get(domain_id)
    if found is None:
        found = (await session.execute(_PARTITION_OF, {"domain": domain_id})).scalar_one()
        _partitions[domain_id] = found
    return found


async def existing_indexes(session: AsyncSession, domain_id: str) -> list[ExistingIndex]:
    """Per-Attribute indexes of a Domain, every branch, valid or not."""
    rows = (await session.execute(_EXISTING, {"domain": domain_id})).mappings()
    found: list[ExistingIndex] = []
    for row in rows:
        meta: dict[str, Any] = json.loads(row["meta"])
        found.append(
            ExistingIndex(
                name=row["name"],
                partition=row["partition"],
                branch=meta["branch"],
                kind_id=meta["kind"],
                attribute_id=meta["attribute"],
                unique=bool(meta["unique"]),
                cast=meta.get("cast"),
                valid=bool(row["valid"]),
            )
        )
    return found


def _lock_key(partition: str) -> int:
    digest = hashlib.sha256(_LOCK_SCOPE + partition.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


# Synchronization --------------------------------------------------------------------------------

_BRANCHES = text("SELECT name FROM refs WHERE domain_id = :domain")
_OWNS_TABLE = text(
    "SELECT pg_has_role(current_user, relowner, 'USAGE') FROM pg_class "
    "WHERE oid = CAST(:table AS regclass)"
)
_warned_not_owner = False
_KIND_ROWS = text(
    "SELECT count(*) FROM mat_records "
    "WHERE domain_id = :domain AND branch = :branch AND kind_id = :kind"
)
_PENDING_JOB = text(
    """
    SELECT 1 FROM jobs
    WHERE domain_id = :domain AND type = :type AND status IN ('queued', 'running')
      AND payload ->> 'name' = :name AND payload ->> 'action' = :action
    LIMIT 1
    """
)
_QUEUE_JOB = text(
    "INSERT INTO jobs (type, domain_id, payload) VALUES (:type, :domain, CAST(:payload AS jsonb))"
)


class IndexAction(StrEnum):
    CREATE = "create"
    DROP = "drop"


class IndexMode(StrEnum):
    """How a session manages the indexes of a branch."""

    INLINE = "inline"
    """The role owns `mat_records`: DDL in the transaction, large Kinds queued."""
    QUEUE = "queue"
    """A maintenance role is configured: every change is queued for the job worker."""
    OFF = "off"
    """Disabled, or nobody may run the DDL: see `pending_index_plan`."""


async def _ddl(session: AsyncSession, statement: str) -> None:
    """Run DDL text as is: literals in it may hold colons that bind parsing would misread."""
    connection = await session.connection()
    await connection.exec_driver_sql(statement)


class BranchIndexes:
    """Keeps the per-Attribute indexes of one branch in line with its schema.

    Runs in the caller's transaction, after `scope_to_domain`. `retire` goes before the
    materialized rows of a schema change are written, `ensure` after them; `sync` does both
    for a branch whose rows are already in place.

    With `maintenance` (a maintenance role is configured, `database.maintenance_dsn`), a
    session role that does not own `mat_records` queues every change instead of skipping it;
    the job worker runs the jobs as the maintenance role.
    """

    def __init__(
        self,
        session: AsyncSession,
        domain_id: str,
        branch: str,
        *,
        settings: IndexSettings,
        maintenance: bool = False,
    ) -> None:
        self._session = session
        self._domain = domain_id
        self._branch = branch
        self._settings = settings
        self._maintenance = maintenance
        self._existing: list[ExistingIndex] | None = None
        self._locked = False
        self._mode: IndexMode | None = None
        self._overlay: bool | None = None

    async def sync(self, schema: Schema) -> IndexSync:
        result = await self.retire(schema)
        ensured = await self.ensure(schema)
        result.created.extend(ensured.created)
        result.queued.extend(ensured.queued)
        return result

    async def retire(self, schema: Schema) -> IndexSync:
        """Drop or queue the drop of indexes the schema no longer plans.

        Inline (in this transaction) for indexes that would reject rows the new schema allows:
        unique indexes and cast expressions of an Attribute whose type changed. Indexes of
        deleted branches of the Domain are queued too.
        """
        result = IndexSync()
        mode = await self.mode()
        if mode is IndexMode.OFF:
            return result
        planned = (await self._plan(schema)).indexes
        wanted = {spec.name(self._domain, self._branch) for spec in planned}
        branches = set((await self._session.execute(_BRANCHES, {"domain": self._domain})).scalars())
        for index in await self._indexes():
            if index.branch == self._branch:
                if index.name in wanted and index.valid:
                    continue
                inline = index.name in wanted or self._blocks_writes(index, schema)
                # An invalid leftover of a failed concurrent build is rebuilt; an index that
                # would reject the new rows goes before they are written.
                if inline and mode is IndexMode.INLINE:
                    await self._drop_inline(index, result)
                    continue
                if inline and index.name not in wanted:
                    log.warning(
                        "index %s may reject the rows of this change until the job worker "
                        "drops it (queued in its own transaction)",
                        index.name,
                    )
                    await self._queue(
                        IndexAction.DROP, index.name, index.kind_id, result, separately=True
                    )
                elif index.name not in wanted:
                    await self._queue(IndexAction.DROP, index.name, index.kind_id, result)
            elif index.branch not in branches:
                await self._queue(IndexAction.DROP, index.name, index.kind_id, result)
        return result

    async def ensure(self, schema: Schema) -> IndexSync:
        """Create the planned indexes that do not exist yet: inline for small Kinds when the
        role owns `mat_records`, otherwise queued. An overlay's are always queued: building
        an index scans the whole partition, which an overlay's creation must not pay."""
        result = IndexSync()
        mode = await self.mode()
        if mode is IndexMode.OFF:
            return result
        present = {index.name for index in await self._indexes() if index.valid}
        partition = await partition_of(self._session, self._domain)
        queue_all = mode is IndexMode.QUEUE or await self.overlay()
        rows: dict[str, int] = {}
        for spec in (await self._plan(schema)).indexes:
            name = spec.name(self._domain, self._branch)
            if name in present:
                continue
            if queue_all:
                await self._queue(IndexAction.CREATE, name, spec.kind_id, result)
                continue
            if spec.kind_id not in rows:
                rows[spec.kind_id] = await self._kind_rows(spec.kind_id)
            if rows[spec.kind_id] > self._settings.inline_max_rows:
                await self._queue(IndexAction.CREATE, name, spec.kind_id, result)
                continue
            await self._lock(partition)
            await _ddl(
                self._session,
                create_index_sql(spec, self._domain, self._branch, partition, concurrently=False),
            )
            await _ddl(self._session, comment_sql(spec, self._domain, self._branch))
            result.created.append(name)
        self._existing = None
        return result

    async def drop_all(self) -> IndexSync:
        """Queue the drop of every index of the branch (for example after deleting it)."""
        result = IndexSync()
        for index in await self._indexes():
            if index.branch == self._branch:
                await self._queue(IndexAction.DROP, index.name, index.kind_id, result)
        return result

    # Internals -----------------------------------------------------------------------------

    async def mode(self) -> IndexMode:
        """Inline when the session role owns `mat_records`, queued when it does not but a
        maintenance role is configured, off otherwise (or when disabled)."""
        global _warned_not_owner
        if self._mode is None:
            mode = IndexMode.OFF
            if self._settings.enabled:
                found = await self._session.execute(_OWNS_TABLE, {"table": PARENT_TABLE})
                if found.scalar_one_or_none():
                    mode = IndexMode.INLINE
                elif self._maintenance:
                    mode = IndexMode.QUEUE
                elif not _warned_not_owner:
                    _warned_not_owner = True
                    log.warning(
                        "per-Attribute indexes are not managed: the database role does not own "
                        "%s and no database.maintenance_dsn is set; an administrator applies "
                        "`python -m lorebase_store.indexes plan --domain <id>`",
                        PARENT_TABLE,
                    )
            self._mode = mode
        return self._mode

    async def overlay(self) -> bool:
        """Whether the branch is an overlay (read from its ref once)."""
        if self._overlay is None:
            self._overlay = (await load_layout(self._session, self._domain, self._branch)).overlay
        return self._overlay

    async def _plan(self, schema: Schema) -> IndexPlan:
        return plan_indexes(schema, overlay=await self.overlay())

    async def _indexes(self) -> list[ExistingIndex]:
        if self._existing is None:
            self._existing = await existing_indexes(self._session, self._domain)
        return self._existing

    def _blocks_writes(self, index: ExistingIndex, schema: Schema) -> bool:
        if index.unique or not index.valid:
            return True
        if index.cast is None:
            return False
        kind = schema.kinds.get(index.kind_id)
        attribute = None if kind is None else kind.attributes.get(index.attribute_id)
        return attribute is not None and _CASTS.get(attribute.type.name) != index.cast

    async def _drop_inline(self, index: ExistingIndex, result: IndexSync) -> None:
        await self._lock(index.partition)
        await _ddl(self._session, drop_index_sql(index.name, concurrently=False))
        result.dropped.append(index.name)
        self._existing = None

    async def _lock(self, partition: str) -> None:
        """Serialize index DDL per partition: two commits that both wrote rows and then wait
        for each other's lock to build an index would deadlock."""
        if not self._locked:
            await self._session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": _lock_key(partition)}
            )
            self._locked = True

    async def _kind_rows(self, kind_id: str) -> int:
        params = {"domain": self._domain, "branch": self._branch, "kind": kind_id}
        return int((await self._session.execute(_KIND_ROWS, params)).scalar_one())

    async def _queue(
        self,
        action: IndexAction,
        name: str,
        kind_id: str,
        result: IndexSync,
        *,
        separately: bool = False,
    ) -> None:
        """Queue a job in the caller's transaction, or with `separately` in a transaction of
        its own that commits now (it must outlive a commit that fails)."""
        if separately:
            engine = (await self._session.connection()).engine
            async with AsyncSession(bind=engine) as other, other.begin():
                await scope_to_domain(other, self._domain)
                await self._insert_job(other, action, name, kind_id, force=True)
        else:
            await self._insert_job(self._session, action, name, kind_id)
        result.queued.append(name)

    async def _insert_job(
        self,
        session: AsyncSession,
        action: IndexAction,
        name: str,
        kind_id: str,
        *,
        force: bool = False,
    ) -> None:
        params = {"domain": self._domain, "type": INDEX_JOB_TYPE, "name": name, "action": action}
        if (await session.execute(_PENDING_JOB, params)).first() is None:
            payload: dict[str, Any] = {
                "action": action.value,
                "name": name,
                "branch": self._branch,
                "kind": kind_id,
            }
            if force:
                payload["force"] = True
            await session.execute(
                _QUEUE_JOB,
                {"type": INDEX_JOB_TYPE, "domain": self._domain, "payload": json.dumps(payload)},
            )


# Queued jobs ------------------------------------------------------------------------------------

_CLAIM = text(
    """
    UPDATE jobs SET status = 'running', locked_by = :worker, locked_at = now(),
                    attempts = attempts + 1
    WHERE id = (
        SELECT id FROM jobs
        WHERE status = 'queued' AND type = :type AND run_after <= now()
        ORDER BY priority DESC, run_after
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id, domain_id, payload
    """
)
_FINISH = text("UPDATE jobs SET status = :status, result = CAST(:result AS jsonb) WHERE id = :id")
_BRANCH_SCHEMA = text(
    "SELECT compiled FROM mat_schema WHERE domain_id = :domain AND branch = :branch"
)
_BRANCH_EXISTS = text("SELECT 1 FROM refs WHERE domain_id = :domain AND name = :branch")
_BYPASS = text("SELECT set_config('lorebase.bypass_rls', 'on', true)")


@dataclass(frozen=True, slots=True)
class IndexJobOutcome:
    job_id: int
    action: IndexAction
    name: str
    status: JobStatus
    detail: str


async def run_index_jobs(
    engine: AsyncEngine, settings: IndexSettings, *, worker: str = "store"
) -> list[IndexJobOutcome]:
    """Claim and run up to `settings.job_batch` queued index jobs.

    Builds and drops run with `CONCURRENTLY`, outside any transaction, after checking that
    the branch still wants (or no longer wants) the index.
    """
    outcomes: list[IndexJobOutcome] = []
    for _ in range(settings.job_batch):
        async with engine.begin() as connection:
            await connection.execute(_BYPASS)
            claimed = (
                await connection.execute(_CLAIM, {"worker": worker, "type": INDEX_JOB_TYPE})
            ).first()
        if claimed is None:
            break
        job_id, domain_id, payload = claimed
        action = IndexAction(payload["action"])
        try:
            status, detail = await _run_job(engine, domain_id, action, payload)
        except Exception as error:  # the job records the failure; the runner goes on
            status, detail = JobStatus.FAILED, f"{type(error).__name__}: {error}"
        async with engine.begin() as connection:
            await connection.execute(_BYPASS)
            await connection.execute(
                _FINISH,
                {"id": job_id, "status": status.value, "result": json.dumps({"detail": detail})},
            )
        outcomes.append(IndexJobOutcome(job_id, action, payload["name"], status, detail))
    return outcomes


async def _run_job(
    engine: AsyncEngine, domain_id: str, action: IndexAction, payload: Mapping[str, Any]
) -> tuple[JobStatus, str]:
    name: str = payload["name"]
    branch: str = payload["branch"]
    async with engine.begin() as connection, AsyncSession(bind=connection) as session:
        await scope_to_domain(session, domain_id)
        found = {"domain": domain_id, "branch": branch}
        if action is IndexAction.CREATE and not await _branch_exists(session, found):
            # Merged and deleted since the job was queued (the branch cleanup cancels the
            # queued ones; this covers a job already claimed or a deletion that raced it).
            log.debug(
                "index job %s: branch %r of Domain %s is gone; nothing to build",
                name,
                branch,
                domain_id,
            )
            return JobStatus.CANCELLED, "the branch no longer exists"
        compiled = (await session.execute(_BRANCH_SCHEMA, found)).scalar_one_or_none()
        partition = await partition_of(session, domain_id)
        existing = {i.name: i for i in await existing_indexes(session, domain_id)}
        overlay = (await load_layout(session, domain_id, branch)).overlay
    spec = None
    if compiled is not None:
        planned = plan_indexes(Schema.model_validate(compiled), overlay=overlay).indexes
        spec = next((s for s in planned if s.name(domain_id, branch) == name), None)
    statements: list[str] = []
    if action is IndexAction.DROP:
        # Jobs are queued by the application role and run by the maintenance role: only a
        # per-Attribute index of this Domain may be dropped, whatever name the job carries.
        if name not in existing:
            return JobStatus.CANCELLED, "not a per-Attribute index of this Domain"
        # Forced: an index that would reject the rows of a pending schema change, whose
        # commit cannot land (and change the plan) while the index exists.
        forced = bool(payload.get("force"))
        if not forced and spec is not None and name in existing and existing[name].valid:
            return JobStatus.CANCELLED, "the branch still plans this index"
        statements.append(drop_index_sql(name, concurrently=True))
    else:
        if spec is None:
            return JobStatus.CANCELLED, "the branch no longer plans this index"
        if name in existing and existing[name].valid:
            return JobStatus.SUCCEEDED, "already built"
        if name in existing:
            statements.append(drop_index_sql(name, concurrently=True))
        statements.append(create_index_sql(spec, domain_id, branch, partition, concurrently=True))
        statements.append(comment_sql(spec, domain_id, branch))
    async with engine.connect() as connection:
        auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
        for statement in statements:
            await auto.exec_driver_sql(statement)
        if action is IndexAction.CREATE and not await _branch_still_exists(
            engine, domain_id, branch
        ):
            # Deleted while the (long) concurrent build ran: do not leave the index behind.
            await auto.exec_driver_sql(drop_index_sql(name, concurrently=True))
            log.debug(
                "index job %s: branch %r of Domain %s was deleted during the build; dropped",
                name,
                branch,
                domain_id,
            )
            return JobStatus.CANCELLED, "the branch was deleted during the build"
    return JobStatus.SUCCEEDED, action.value


async def _branch_exists(session: AsyncSession, params: Mapping[str, str]) -> bool:
    return (await session.execute(_BRANCH_EXISTS, params)).first() is not None


async def _branch_still_exists(engine: AsyncEngine, domain_id: str, branch: str) -> bool:
    async with engine.begin() as connection, AsyncSession(bind=connection) as session:
        await scope_to_domain(session, domain_id)
        return await _branch_exists(session, {"domain": domain_id, "branch": branch})


async def run_index_job(engine: AsyncEngine, job: JobRow) -> dict[str, Any]:
    """Handler of `store.index` jobs for `lorebase_store.jobs.run_jobs`.

    `engine` runs the DDL: the maintenance engine (`database.maintenance_dsn`) where the
    application role does not own `mat_records`. A failure raises, so the queue retries it.
    """
    if job.domain_id is None:
        raise ValueError(f"index job {job.id} has no Domain")
    action = IndexAction(job.payload["action"])
    status, detail = await _run_job(engine, job.domain_id, action, job.payload)
    return {"status": status.value, "detail": detail, "name": job.payload["name"]}


# Pending plan -----------------------------------------------------------------------------------

_SCHEMAS = text(
    "SELECT r.name, s.compiled, r.materialization = 'overlay' FROM refs r "
    "JOIN mat_schema s ON s.domain_id = r.domain_id AND s.branch = r.name "
    "WHERE r.domain_id = :domain ORDER BY r.name"
)


async def pending_index_plan(
    session: AsyncSession, domain_id: str, *, branch: str | None = None
) -> list[str]:
    """DDL that brings the per-Attribute indexes of a Domain (or of one branch) in line with
    the materialized schemas: concurrent builds of missing or invalid indexes with their
    comments, and concurrent drops of indexes that no branch plans.

    For an administrator where no role that may run the DDL manages the indexes; each
    statement runs outside a transaction. Runs in the caller's transaction, after
    `scope_to_domain`, and only reads.
    """
    found = (await session.execute(_SCHEMAS, {"domain": domain_id})).all()
    schemas = {
        name: (Schema.model_validate(compiled), bool(overlay))
        for name, compiled, overlay in found
        if branch is None or name == branch
    }
    existing = {index.name: index for index in await existing_indexes(session, domain_id)}
    branches = set((await session.execute(_BRANCHES, {"domain": domain_id})).scalars())
    partition = await partition_of(session, domain_id)
    statements: list[str] = []
    wanted: set[str] = set()
    for name, (schema, overlay) in schemas.items():
        for spec in plan_indexes(schema, overlay=overlay).indexes:
            index_name = spec.name(domain_id, name)
            wanted.add(index_name)
            current = existing.get(index_name)
            if current is not None and current.valid:
                continue
            if current is not None:
                statements.append(drop_index_sql(index_name, concurrently=True))
            statements.append(create_index_sql(spec, domain_id, name, partition, concurrently=True))
            statements.append(comment_sql(spec, domain_id, name))
    for index in existing.values():
        if index.name in wanted:
            continue
        if index.branch in schemas or (branch is None and index.branch not in branches):
            statements.append(drop_index_sql(index.name, concurrently=True))
    return statements


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lorebase_store.indexes",
        description="Per-Attribute indexes of mat_records.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser(
        "plan", help="Print the DDL that brings a Domain's indexes in line with its schemas."
    )
    plan.add_argument("--domain", required=True, help="Domain id.")
    plan.add_argument("--branch", default=None, help="Only this branch (default: every one).")
    return parser


async def _plan(domain_id: str, branch: str | None) -> list[str]:
    from lorebase_platform.settings import load_settings
    from lorebase_store.db import create_engine
    from lorebase_store.settings import database_settings

    engine = create_engine(database_settings(load_settings()), pooled=False)
    try:
        async with engine.begin() as connection, AsyncSession(bind=connection) as session:
            await scope_to_domain(session, domain_id)
            return await pending_index_plan(session, domain_id, branch=branch)
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """`plan`: print the pending DDL, one statement per line, for psql."""
    args = _parser().parse_args(argv)
    for statement in asyncio.run(_plan(args.domain, args.branch)):
        sys.stdout.write(statement + ";\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
