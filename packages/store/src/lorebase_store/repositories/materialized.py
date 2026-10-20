"""Materialized head of one branch: Records, Links, history and compiled schema (design 7.2)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Table,
    and_,
    bindparam,
    cast,
    delete,
    func,
    insert,
    literal,
    literal_column,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import REGCONFIG
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.bulk import OnConflict, bulk_write, chunks
from lorebase_store.models import metadata
from lorebase_store.settings import WriteSettings

MAT_SCHEMA: Table = metadata.tables["mat_schema"]
MAT_RECORDS: Table = metadata.tables["mat_records"]
MAT_LINKS: Table = metadata.tables["mat_links"]
MAT_HISTORY: Table = metadata.tables["mat_history"]
REVISIONS: Table = metadata.tables["revisions"]

SEARCH_WEIGHTS = ("A", "B", "C", "D")
"""Valid full-text weights (Postgres `setweight`)."""

_RECORD_UPDATE_COLUMNS = (
    "values",
    "title",
    "object_hash",
    "lineage",
    "updated_revision",
    "updated_at",
)
_LINK_UPDATE_COLUMNS = ("values", "object_hash", "lineage")

_REBUILD_HISTORY = text(
    """
    WITH RECURSIVE chain(id) AS (
        SELECT CAST(:head AS bytea)
        UNION ALL
        SELECT p.parent_id
        FROM chain
        JOIN revision_parents p
          ON p.domain_id = :domain AND p.revision_id = chain.id AND p.position = 0
    )
    INSERT INTO mat_history
        (domain_id, branch, partition, element_key, revision_id, at, old_hash, new_hash,
         provenance)
    SELECT :domain, :branch, c.partition, c.element_key, c.revision_id, r.created_at,
           c.old_hash, c.new_hash, c.provenance
    FROM chain
    JOIN revisions r ON r.domain_id = :domain AND r.id = chain.id
    JOIN changesets c ON c.domain_id = :domain AND c.revision_id = chain.id
    """
)


@dataclass(frozen=True, slots=True)
class RecordRow:
    kind_id: str
    key: str
    values: dict[str, Any]
    title: str | None
    object_hash: bytes
    lineage: dict[str, str]
    updated_revision: bytes
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class LinkRow:
    relation_id: str
    from_key: str
    to_key: str
    values: dict[str, Any]
    object_hash: bytes
    lineage: dict[str, str]


@dataclass(frozen=True, slots=True)
class HistoryRow:
    partition: str
    element_key: str
    revision_id: bytes
    at: datetime
    old_hash: bytes | None
    new_hash: bytes | None
    provenance: dict[str, str] | None


@dataclass(frozen=True, slots=True)
class SchemaRow:
    schema_hash: bytes
    compiled: dict[str, Any]


@dataclass(frozen=True, slots=True)
class HistoryStep:
    """One change of an element along the branch, in generation order."""

    element_key: str
    revision_id: bytes
    at: datetime
    new_hash: bytes | None
    provenance: dict[str, str] | None


def search_vector(config: str, weights: Sequence[tuple[str, str]]) -> ColumnElement[Any]:
    """Search vector of a Record row: its title at weight A plus searchable Attributes.

    `weights` lists `(attribute_id, weight)`; values are read as text from `values`.
    """
    regconfig = cast(literal(config), REGCONFIG)
    vector: ColumnElement[Any] = func.setweight(
        func.to_tsvector(regconfig, func.coalesce(MAT_RECORDS.c.title, "")),
        literal_column("'A'"),
    )
    for position, (attribute_id, weight) in enumerate(weights):
        if weight not in SEARCH_WEIGHTS:
            raise ValueError(f"invalid search weight {weight!r}")
        value = MAT_RECORDS.c["values"].op("->>")(
            bindparam(f"search_attr_{position}", attribute_id)
        )
        part = func.setweight(
            func.to_tsvector(regconfig, func.coalesce(value, "")), literal_column(f"'{weight}'")
        )
        vector = vector.op("||")(part)
    return vector


class MaterializedRepository:
    """Rows of the materialized head of one branch."""

    def __init__(
        self, session: AsyncSession, domain_id: str, branch: str, *, writes: WriteSettings
    ) -> None:
        self._session = session
        self._domain = domain_id
        self._branch = branch
        self._writes = writes

    @property
    def branch(self) -> str:
        return self._branch

    def _scope(self, table: Table) -> ColumnElement[bool]:
        return and_(table.c.domain_id == self._domain, table.c.branch == self._branch)

    # Schema -------------------------------------------------------------------------

    async def schema(self) -> SchemaRow | None:
        result = await self._session.execute(
            select(MAT_SCHEMA.c.schema_hash, MAT_SCHEMA.c.compiled).where(self._scope(MAT_SCHEMA))
        )
        found = result.one_or_none()
        return None if found is None else SchemaRow(found[0], found[1])

    async def set_schema(self, row: SchemaRow) -> None:
        await bulk_write(
            self._session,
            MAT_SCHEMA,
            [
                {
                    "domain_id": self._domain,
                    "branch": self._branch,
                    "schema_hash": row.schema_hash,
                    "compiled": row.compiled,
                }
            ],
            writes=self._writes,
            on_conflict=OnConflict.UPDATE,
            update_columns=("schema_hash", "compiled"),
        )

    # Records --------------------------------------------------------------------------

    async def records(self, kind_id: str, keys: Iterable[str]) -> dict[str, RecordRow]:
        found: dict[str, RecordRow] = {}
        for chunk in chunks(list(dict.fromkeys(keys)), self._writes.insert_batch_rows):
            result = await self._session.execute(
                self._record_select().where(
                    self._scope(MAT_RECORDS),
                    MAT_RECORDS.c.kind_id == kind_id,
                    MAT_RECORDS.c.key.in_(chunk),
                )
            )
            for row in result.mappings():
                found[row["key"]] = RecordRow(**row)
        return found

    async def upsert_records(self, rows: Sequence[RecordRow]) -> None:
        await bulk_write(
            self._session,
            MAT_RECORDS,
            [self._record_values(row) for row in rows],
            writes=self._writes,
            on_conflict=OnConflict.UPDATE,
            update_columns=_RECORD_UPDATE_COLUMNS,
        )

    async def delete_records(self, kind_id: str, keys: Sequence[str]) -> None:
        for chunk in chunks(keys, self._writes.insert_batch_rows):
            await self._session.execute(
                delete(MAT_RECORDS).where(
                    self._scope(MAT_RECORDS),
                    MAT_RECORDS.c.kind_id == kind_id,
                    MAT_RECORDS.c.key.in_(chunk),
                )
            )

    async def update_titles(self, kind_id: str, titles: Sequence[tuple[str, str | None]]) -> None:
        statement = (
            update(MAT_RECORDS)
            .where(
                self._scope(MAT_RECORDS),
                MAT_RECORDS.c.kind_id == kind_id,
                MAT_RECORDS.c.key == bindparam("record_key"),
            )
            .values(title=bindparam("record_title"))
        )
        for chunk in chunks(titles, self._writes.insert_batch_rows):
            await self._session.execute(
                statement, [{"record_key": key, "record_title": title} for key, title in chunk]
            )

    async def refresh_search(
        self,
        kind_id: str,
        weights: Sequence[tuple[str, str]],
        *,
        config: str,
        revision: bytes | None = None,
    ) -> None:
        """Recompute search vectors of a Kind's rows (only those of `revision` when given)."""
        statement = (
            update(MAT_RECORDS)
            .where(self._scope(MAT_RECORDS), MAT_RECORDS.c.kind_id == kind_id)
            .values(search=search_vector(config, weights))
        )
        if revision is not None:
            statement = statement.where(MAT_RECORDS.c.updated_revision == revision)
        await self._session.execute(statement)

    async def stream_records(self, kind_id: str, *, batch: int) -> AsyncIterator[list[RecordRow]]:
        """All Records of a Kind in key order, `batch` rows at a time."""
        last: str | None = None
        while True:
            query = (
                self._record_select()
                .where(self._scope(MAT_RECORDS), MAT_RECORDS.c.kind_id == kind_id)
                .order_by(MAT_RECORDS.c.key)
                .limit(batch)
            )
            if last is not None:
                query = query.where(MAT_RECORDS.c.key > last)
            rows = [RecordRow(**row) for row in (await self._session.execute(query)).mappings()]
            if not rows:
                return
            yield rows
            last = rows[-1].key

    async def stream_record_hashes(
        self, kind_id: str, *, batch: int
    ) -> AsyncIterator[list[tuple[str, bytes]]]:
        """`(key, object_hash)` of a Kind's Records in key order."""
        last: str | None = None
        while True:
            query = (
                select(MAT_RECORDS.c.key, MAT_RECORDS.c.object_hash)
                .where(self._scope(MAT_RECORDS), MAT_RECORDS.c.kind_id == kind_id)
                .order_by(MAT_RECORDS.c.key)
                .limit(batch)
            )
            if last is not None:
                query = query.where(MAT_RECORDS.c.key > last)
            rows = [(key, digest) for key, digest in (await self._session.execute(query))]
            if not rows:
                return
            yield rows
            last = rows[-1][0]

    async def record_counts(self) -> dict[str, int]:
        """Number of Records per Kind id."""
        result = await self._session.execute(
            select(MAT_RECORDS.c.kind_id, func.count())
            .where(self._scope(MAT_RECORDS))
            .group_by(MAT_RECORDS.c.kind_id)
        )
        return {kind_id: int(count) for kind_id, count in result}

    # Links --------------------------------------------------------------------------

    async def links(
        self, relation_id: str, pairs: Iterable[tuple[str, str]]
    ) -> dict[tuple[str, str], LinkRow]:
        found: dict[tuple[str, str], LinkRow] = {}
        for chunk in chunks(list(dict.fromkeys(pairs)), self._writes.insert_batch_rows):
            result = await self._session.execute(
                self._link_select().where(
                    self._scope(MAT_LINKS),
                    MAT_LINKS.c.relation_id == relation_id,
                    tuple_(MAT_LINKS.c.from_key, MAT_LINKS.c.to_key).in_(chunk),
                )
            )
            for row in result.mappings():
                found[(row["from_key"], row["to_key"])] = LinkRow(**row)
        return found

    async def upsert_links(self, rows: Sequence[LinkRow]) -> None:
        await bulk_write(
            self._session,
            MAT_LINKS,
            [self._link_values(row) for row in rows],
            writes=self._writes,
            on_conflict=OnConflict.UPDATE,
            update_columns=_LINK_UPDATE_COLUMNS,
        )

    async def delete_links(self, relation_id: str, pairs: Sequence[tuple[str, str]]) -> None:
        for chunk in chunks(pairs, self._writes.insert_batch_rows):
            await self._session.execute(
                delete(MAT_LINKS).where(
                    self._scope(MAT_LINKS),
                    MAT_LINKS.c.relation_id == relation_id,
                    tuple_(MAT_LINKS.c.from_key, MAT_LINKS.c.to_key).in_(chunk),
                )
            )

    async def stream_links(self, relation_id: str, *, batch: int) -> AsyncIterator[list[LinkRow]]:
        """All Links of a Relation in `(from_key, to_key)` order."""
        last: tuple[str, str] | None = None
        while True:
            query = (
                self._link_select()
                .where(self._scope(MAT_LINKS), MAT_LINKS.c.relation_id == relation_id)
                .order_by(MAT_LINKS.c.from_key, MAT_LINKS.c.to_key)
                .limit(batch)
            )
            if last is not None:
                query = query.where(tuple_(MAT_LINKS.c.from_key, MAT_LINKS.c.to_key) > last)
            rows = [LinkRow(**row) for row in (await self._session.execute(query)).mappings()]
            if not rows:
                return
            yield rows
            last = (rows[-1].from_key, rows[-1].to_key)

    async def link_counts(self) -> dict[str, int]:
        """Number of Links per Relation id."""
        result = await self._session.execute(
            select(MAT_LINKS.c.relation_id, func.count())
            .where(self._scope(MAT_LINKS))
            .group_by(MAT_LINKS.c.relation_id)
        )
        return {relation_id: int(count) for relation_id, count in result}

    # History ------------------------------------------------------------------------

    async def insert_history(self, rows: Sequence[HistoryRow]) -> None:
        await bulk_write(
            self._session,
            MAT_HISTORY,
            [
                {
                    "domain_id": self._domain,
                    "branch": self._branch,
                    "partition": row.partition,
                    "element_key": row.element_key,
                    "revision_id": row.revision_id,
                    "at": row.at,
                    "old_hash": row.old_hash,
                    "new_hash": row.new_hash,
                    "provenance": row.provenance,
                }
                for row in rows
            ],
            writes=self._writes,
            on_conflict=OnConflict.ERROR,
        )

    async def history(self, partition: str, element_key: str) -> list[HistoryRow]:
        """Changes of one element on this branch, oldest first."""
        result = await self._session.execute(
            select(
                MAT_HISTORY.c.partition,
                MAT_HISTORY.c.element_key,
                MAT_HISTORY.c.revision_id,
                MAT_HISTORY.c.at,
                MAT_HISTORY.c.old_hash,
                MAT_HISTORY.c.new_hash,
                MAT_HISTORY.c.provenance,
            )
            .join(
                REVISIONS,
                and_(
                    REVISIONS.c.domain_id == MAT_HISTORY.c.domain_id,
                    REVISIONS.c.id == MAT_HISTORY.c.revision_id,
                ),
            )
            .where(
                self._scope(MAT_HISTORY),
                MAT_HISTORY.c.partition == partition,
                MAT_HISTORY.c.element_key == element_key,
            )
            .order_by(REVISIONS.c.generation)
        )
        return [HistoryRow(**row) for row in result.mappings()]

    async def rebuild_history(self, head: bytes) -> None:
        """Replace this branch's history with the changesets of `head`'s first-parent chain."""
        await self._session.execute(delete(MAT_HISTORY).where(self._scope(MAT_HISTORY)))
        await self._session.execute(
            _REBUILD_HISTORY, {"head": head, "domain": self._domain, "branch": self._branch}
        )

    async def stream_history_steps(
        self, partition: str, *, batch: int
    ) -> AsyncIterator[list[HistoryStep]]:
        """History of a partition ordered by element key, then oldest change first."""
        query = (
            select(
                MAT_HISTORY.c.element_key,
                MAT_HISTORY.c.revision_id,
                MAT_HISTORY.c.at,
                MAT_HISTORY.c.new_hash,
                MAT_HISTORY.c.provenance,
            )
            .join(
                REVISIONS,
                and_(
                    REVISIONS.c.domain_id == MAT_HISTORY.c.domain_id,
                    REVISIONS.c.id == MAT_HISTORY.c.revision_id,
                ),
            )
            .where(self._scope(MAT_HISTORY), MAT_HISTORY.c.partition == partition)
            .order_by(MAT_HISTORY.c.element_key, REVISIONS.c.generation)
        )
        result = await self._session.stream(query)
        async for part in result.partitions(batch):
            yield [HistoryStep(*row) for row in part]

    # Whole branch -------------------------------------------------------------------

    async def clear(self) -> None:
        """Delete every materialized row of the branch."""
        for target in (MAT_RECORDS, MAT_LINKS, MAT_HISTORY, MAT_SCHEMA):
            await self._session.execute(delete(target).where(self._scope(target)))

    async def copy_from(self, source_branch: str) -> None:
        """Copy every materialized row of `source_branch` into this (empty) branch."""
        for target in (MAT_SCHEMA, MAT_RECORDS, MAT_LINKS, MAT_HISTORY):
            names = [c.name for c in target.columns]
            source = select(
                *(
                    literal(self._branch).label("branch") if name == "branch" else target.c[name]
                    for name in names
                )
            ).where(target.c.domain_id == self._domain, target.c.branch == source_branch)
            await self._session.execute(insert(target).from_select(names, source))

    # Row mapping --------------------------------------------------------------------

    @staticmethod
    def _record_select() -> Any:
        c = MAT_RECORDS.c
        return select(
            c.kind_id,
            c.key,
            c["values"],
            c.title,
            c.object_hash,
            c.lineage,
            c.updated_revision,
            c.updated_at,
        )

    @staticmethod
    def _link_select() -> Any:
        c = MAT_LINKS.c
        return select(c.relation_id, c.from_key, c.to_key, c["values"], c.object_hash, c.lineage)

    def _record_values(self, row: RecordRow) -> dict[str, Any]:
        return {
            "domain_id": self._domain,
            "branch": self._branch,
            "kind_id": row.kind_id,
            "key": row.key,
            "values": row.values,
            "title": row.title,
            "object_hash": row.object_hash,
            "lineage": row.lineage,
            "updated_revision": row.updated_revision,
            "updated_at": row.updated_at,
        }

    def _link_values(self, row: LinkRow) -> dict[str, Any]:
        return {
            "domain_id": self._domain,
            "branch": self._branch,
            "relation_id": row.relation_id,
            "from_key": row.from_key,
            "to_key": row.to_key,
            "values": row.values,
            "object_hash": row.object_hash,
            "lineage": row.lineage,
        }
