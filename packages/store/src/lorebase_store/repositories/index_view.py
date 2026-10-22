"""Core `IndexView` backed by the materialized head of a branch.

Validation (core `evaluate_changeset`) asks one question at a time. To keep a batch of
thousands of changes at a handful of queries, `prefetch_for` loads in bulk what the batch will
ask (existence and state of touched Records and Links, unique values, Link counts and Links of
deleted Records); the view then answers from memory and falls back to one query per miss.

Like `PgNodeStore`, the view runs over a synchronous `Session` inside `AsyncSession.run_sync`.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import JsonValue
from sqlalchemy import ColumnElement, FromClause, TextClause, and_, func, select, text, tuple_
from sqlalchemy.orm import Session

from lorebase_core.model import Link, Record, RecordIdentity, RelationBacking, Schema
from lorebase_core.model.record import LinkIdentity
from lorebase_core.types import AttributeType, CanonicalValue
from lorebase_core.validate import DataChange, LinkUpsert, RecordDelete, RecordUpsert
from lorebase_store.bulk import chunks, pairs_in
from lorebase_store.layout import BranchLayout, load_layout_sync

Side = Literal["from", "to"]

_UNIQUE_VALUES = """
    SELECT key, "values" -> :attribute AS value
    FROM {records}
    WHERE domain_id = :domain AND branch = :branch AND kind_id = :kind
      AND "values" -> :attribute IN (SELECT jsonb_array_elements(CAST(:candidates AS jsonb)))
    """
_REFERENCING = """
    SELECT key, "values" -> :attribute AS value
    FROM {records}
    WHERE domain_id = :domain AND branch = :branch AND kind_id = :kind
      AND jsonb_exists_any("values" -> :attribute, CAST(:keys AS text[]))
    """


@dataclass(frozen=True, slots=True)
class RecordState:
    """A Record as materialized before the batch."""

    values: dict[str, Any]
    lineage: dict[str, str]
    object_hash: bytes


@dataclass(frozen=True, slots=True)
class LinkState:
    """A Link as materialized before the batch."""

    values: dict[str, Any]
    lineage: dict[str, str]
    object_hash: bytes


@dataclass
class IndexStats:
    queries: int = 0


def value_key(value: JsonValue) -> str:
    """Memo key of a canonical value."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _keys_in(value: JsonValue) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


class MaterializedIndexView:
    """`IndexView` over `mat_records` and `mat_links` of one branch, with bulk prefetch.

    An overlay branch is read through its resolved rows (`lorebase_store.layout`).
    """

    def __init__(
        self,
        session: Session,
        domain_id: str,
        branch: str,
        *,
        batch: int,
        layout: BranchLayout | None = None,
    ) -> None:
        self._session = session
        self._domain = domain_id
        self._branch = branch
        self._batch = batch
        self._layout = layout
        self._records: dict[tuple[str, str], RecordState | None] = {}
        self._links: dict[tuple[str, str, str], LinkState | None] = {}
        self._unique: dict[tuple[str, str, str], str | None] = {}
        self._counts: dict[tuple[str, Side, str], int] = {}
        self._pairs: dict[tuple[str, Side, str], list[tuple[str, str]]] = {}
        self._referencing: dict[tuple[str, str, str], set[str]] = {}
        self.stats = IndexStats()

    # IndexView protocol ---------------------------------------------------------------

    def record_exists(self, kind_id: str, key: str) -> bool:
        return self.record_state(kind_id, key) is not None

    def find_unique(self, kind_id: str, attribute_id: str, value: CanonicalValue) -> str | None:
        memo = (kind_id, attribute_id, value_key(value))
        if memo not in self._unique:
            self.prefetch_unique(kind_id, attribute_id, [value])
        return self._unique.get(memo)

    def keys_referencing(self, kind_id: str, attribute_id: str, key: str) -> Iterable[str]:
        memo = (kind_id, attribute_id, key)
        if memo not in self._referencing:
            self.prefetch_referencing(kind_id, attribute_id, [key])
        return sorted(self._referencing[memo])

    def link_exists(self, relation_id: str, from_key: str, to_key: str) -> bool:
        return self.link_state(relation_id, from_key, to_key) is not None

    def count_links(
        self, relation_id: str, *, from_key: str | None = None, to_key: str | None = None
    ) -> int:
        side, key = _side(from_key, to_key)
        if side is None:
            return self._count_all(relation_id, from_key, to_key)
        memo = (relation_id, side, key)
        if memo not in self._counts:
            self.prefetch_link_counts(relation_id, side, [key])
        return self._counts[memo]

    def links_of(
        self, relation_id: str, *, from_key: str | None = None, to_key: str | None = None
    ) -> Iterable[tuple[str, str]]:
        side, key = _side(from_key, to_key)
        if side is None:
            return [
                pair
                for pair in self._pairs_query(relation_id, None, [])
                if (from_key is None or pair[0] == from_key)
                and (to_key is None or pair[1] == to_key)
            ]
        memo = (relation_id, side, key)
        if memo not in self._pairs:
            self.prefetch_links_of(relation_id, side, [key])
        return list(self._pairs[memo])

    # State before the batch -------------------------------------------------------------

    def record_state(self, kind_id: str, key: str) -> RecordState | None:
        memo = (kind_id, key)
        if memo not in self._records:
            self.prefetch_records([RecordIdentity(kind_id, key)])
        return self._records[memo]

    def link_state(self, relation_id: str, from_key: str, to_key: str) -> LinkState | None:
        memo = (relation_id, from_key, to_key)
        if memo not in self._links:
            self.prefetch_links([LinkIdentity(relation_id, from_key, to_key)])
        return self._links[memo]

    def records_of_kind(self, kind_id: str) -> Iterator[Record]:
        """Every materialized Record of a Kind, in key order."""
        rows_from = self._record_rows()
        last: str | None = None
        while True:
            query = (
                select(rows_from.c.key, rows_from.c["values"])
                .where(self._scope(rows_from), rows_from.c.kind_id == kind_id)
                .order_by(rows_from.c.key)
                .limit(self._batch)
            )
            if last is not None:
                query = query.where(rows_from.c.key > last)
            rows = list(self._execute(query))
            if not rows:
                return
            for key, values in rows:
                yield Record(kind=kind_id, key=key, values=values)
            last = rows[-1][0]

    def links_of_relation(self, relation_id: str) -> Iterator[Link]:
        """Every materialized Link of a Relation, in `(from, to)` order."""
        links = self._link_rows()
        last: tuple[str, str] | None = None
        while True:
            query = (
                select(links.c.from_key, links.c.to_key, links.c["values"])
                .where(self._scope(links), links.c.relation_id == relation_id)
                .order_by(links.c.from_key, links.c.to_key)
                .limit(self._batch)
            )
            if last is not None:
                query = query.where(tuple_(links.c.from_key, links.c.to_key) > last)
            rows = list(self._execute(query))
            if not rows:
                return
            for from_key, to_key, values in rows:
                yield Link(relation=relation_id, from_key=from_key, to_key=to_key, values=values)
            last = (rows[-1][0], rows[-1][1])

    # Bulk prefetch ----------------------------------------------------------------------

    def prefetch_records(self, identities: Iterable[RecordIdentity]) -> None:
        by_kind: dict[str, list[str]] = defaultdict(list)
        for kind_id, key in dict.fromkeys(identities):
            if (kind_id, key) not in self._records:
                by_kind[kind_id].append(key)
        rows = self._record_rows()
        for kind_id, keys in by_kind.items():
            for chunk in chunks(keys, self._batch):
                for key in chunk:
                    self._records[(kind_id, key)] = None
                query = select(
                    rows.c.key,
                    rows.c["values"],
                    rows.c.lineage,
                    rows.c.object_hash,
                ).where(
                    self._scope(rows),
                    rows.c.kind_id == kind_id,
                    rows.c.key.in_(chunk),
                )
                for key, values, lineage, digest in self._execute(query):
                    self._records[(kind_id, key)] = RecordState(values, lineage, digest)

    def prefetch_links(self, identities: Iterable[LinkIdentity]) -> None:
        by_relation: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for relation_id, from_key, to_key in dict.fromkeys(identities):
            if (relation_id, from_key, to_key) not in self._links:
                by_relation[relation_id].append((from_key, to_key))
        links = self._link_rows()
        for relation_id, pairs in by_relation.items():
            for chunk in chunks(pairs, self._batch):
                for from_key, to_key in chunk:
                    self._links[(relation_id, from_key, to_key)] = None
                query = select(
                    links.c.from_key,
                    links.c.to_key,
                    links.c["values"],
                    links.c.lineage,
                    links.c.object_hash,
                ).where(
                    self._scope(links),
                    links.c.relation_id == relation_id,
                    pairs_in(links.c.from_key, links.c.to_key, chunk),
                )
                for from_key, to_key, values, lineage, digest in self._execute(query):
                    self._links[(relation_id, from_key, to_key)] = LinkState(
                        values, lineage, digest
                    )

    def prefetch_unique(
        self, kind_id: str, attribute_id: str, values: Iterable[CanonicalValue]
    ) -> None:
        pending = {
            value_key(v): v
            for v in values
            if (kind_id, attribute_id, value_key(v)) not in self._unique
        }
        for chunk in chunks(list(pending), self._batch):
            for memo in chunk:
                self._unique[(kind_id, attribute_id, memo)] = None
            rows = self._execute(
                self._text(_UNIQUE_VALUES),
                {
                    "domain": self._domain,
                    "branch": self._branch,
                    "kind": kind_id,
                    "attribute": attribute_id,
                    "candidates": json.dumps([pending[memo] for memo in chunk]),
                },
            )
            for key, value in rows:
                self._unique[(kind_id, attribute_id, value_key(value))] = key

    def prefetch_referencing(self, kind_id: str, attribute_id: str, keys: Iterable[str]) -> None:
        pending = [
            k for k in dict.fromkeys(keys) if (kind_id, attribute_id, k) not in self._referencing
        ]
        for chunk in chunks(pending, self._batch):
            for key in chunk:
                self._referencing[(kind_id, attribute_id, key)] = set()
            rows = self._execute(
                self._text(_REFERENCING),
                {
                    "domain": self._domain,
                    "branch": self._branch,
                    "kind": kind_id,
                    "attribute": attribute_id,
                    "keys": list(chunk),
                },
            )
            wanted = set(chunk)
            for record_key, value in rows:
                for target in _keys_in(value):
                    if target in wanted:
                        self._referencing[(kind_id, attribute_id, target)].add(record_key)

    def prefetch_link_counts(self, relation_id: str, side: Side, keys: Iterable[str]) -> None:
        links = self._link_rows()
        column = links.c.from_key if side == "from" else links.c.to_key
        pending = [k for k in dict.fromkeys(keys) if (relation_id, side, k) not in self._counts]
        for chunk in chunks(pending, self._batch):
            for key in chunk:
                self._counts[(relation_id, side, key)] = 0
            query = (
                select(column, func.count())
                .where(
                    self._scope(links),
                    links.c.relation_id == relation_id,
                    column.in_(chunk),
                )
                .group_by(column)
            )
            for key, count in self._execute(query):
                self._counts[(relation_id, side, key)] = int(count)

    def prefetch_links_of(self, relation_id: str, side: Side, keys: Iterable[str]) -> None:
        pending = [k for k in dict.fromkeys(keys) if (relation_id, side, k) not in self._pairs]
        for chunk in chunks(pending, self._batch):
            for key in chunk:
                self._pairs[(relation_id, side, key)] = []
            for pair in self._pairs_query(relation_id, side, chunk):
                key = pair[0] if side == "from" else pair[1]
                self._pairs[(relation_id, side, key)].append(pair)
            for key in chunk:
                self._counts[(relation_id, side, key)] = len(self._pairs[(relation_id, side, key)])

    def prefetch_for(self, schema: Schema, changes: Sequence[DataChange]) -> None:
        """Load in bulk what validating `changes` against `schema` will read."""
        records: list[RecordIdentity] = []
        links: list[LinkIdentity] = []
        unique: dict[tuple[str, str], list[CanonicalValue]] = defaultdict(list)
        counts: dict[tuple[str, Side], list[str]] = defaultdict(list)
        pairs: dict[tuple[str, Side], list[str]] = defaultdict(list)
        referencing: dict[tuple[str, str], list[str]] = defaultdict(list)
        new_records: dict[str, list[str]] = defaultdict(list)
        for change in changes:
            if isinstance(change, RecordUpsert):
                record = change.record
                records.append(record.identity)
                new_records[record.kind].append(record.key)
                kind = schema.kinds.get(record.kind)
                if kind is None:
                    continue
                for attribute in kind.attributes.values():
                    value = record.values.get(attribute.id)
                    if value is None:
                        continue
                    if attribute.unique:
                        unique[(kind.id, attribute.id)].append(value)
                    target = attribute.type.scalar.kind_ref
                    if attribute.type.scalar.name is AttributeType.REF and target is not None:
                        records.extend(RecordIdentity(target, k) for k in _keys_in(value))
            elif isinstance(change, RecordDelete):
                records.append(change.identity)
                for relation in schema.relations_of(change.kind):
                    if relation.backing is not RelationBacking.EXPLICIT:
                        continue
                    if relation.from_kind == change.kind:
                        pairs[(relation.id, "from")].append(change.key)
                    if relation.to_kind == change.kind:
                        pairs[(relation.id, "to")].append(change.key)
                for kind in schema.kinds.values():
                    for attribute in kind.attributes.values():
                        scalar = attribute.type.scalar
                        if scalar.name is AttributeType.REF and scalar.kind_ref == change.kind:
                            referencing[(kind.id, attribute.id)].append(change.key)
            else:
                identity = change.identity
                links.append(identity)
                linked = schema.relations.get(identity.relation)
                if linked is None:
                    continue
                records.append(RecordIdentity(linked.from_kind, identity.from_key))
                records.append(RecordIdentity(linked.to_kind, identity.to_key))
                if linked.single_per_from or linked.required_from:
                    counts[(linked.id, "from")].append(identity.from_key)
                if linked.single_per_to and isinstance(change, LinkUpsert):
                    counts[(linked.id, "to")].append(identity.to_key)
        for required in schema.relations.values():
            if required.required_from:
                counts[(required.id, "from")].extend(new_records.get(required.from_kind, ()))
        self.prefetch_records(records)
        self.prefetch_links(links)
        for (kind_id, attribute_id), values in unique.items():
            self.prefetch_unique(kind_id, attribute_id, values)
        for (relation_id, side), keys in pairs.items():
            self.prefetch_links_of(relation_id, side, keys)
        for (relation_id, side), keys in counts.items():
            self.prefetch_link_counts(relation_id, side, keys)
        for (kind_id, attribute_id), keys in referencing.items():
            self.prefetch_referencing(kind_id, attribute_id, keys)

    # Queries ----------------------------------------------------------------------------

    def layout(self) -> BranchLayout:
        if self._layout is None:
            self._layout = load_layout_sync(self._session, self._domain, self._branch)
        return self._layout

    def _record_rows(self) -> FromClause:
        return self.layout().records()

    def _link_rows(self) -> FromClause:
        return self.layout().links()

    def _text(self, template: str) -> TextClause:
        return text(template.format(records=self.layout().records_from()))

    def _scope(self, table: Any) -> ColumnElement[bool]:
        return and_(table.c.domain_id == self._domain, table.c.branch == self._branch)

    def _execute(self, statement: Any, parameters: dict[str, Any] | None = None) -> Any:
        self.stats.queries += 1
        return self._session.execute(statement, parameters or {})

    def _pairs_query(
        self, relation_id: str, side: Side | None, keys: Sequence[str]
    ) -> list[tuple[str, str]]:
        links = self._link_rows()
        query = select(links.c.from_key, links.c.to_key).where(
            self._scope(links), links.c.relation_id == relation_id
        )
        if side == "from":
            query = query.where(links.c.from_key.in_(keys))
        elif side == "to":
            query = query.where(links.c.to_key.in_(keys))
        return [(f, t) for f, t in self._execute(query)]

    def _count_all(self, relation_id: str, from_key: str | None, to_key: str | None) -> int:
        links = self._link_rows()
        query = select(func.count()).where(self._scope(links), links.c.relation_id == relation_id)
        if from_key is not None:
            query = query.where(links.c.from_key == from_key)
        if to_key is not None:
            query = query.where(links.c.to_key == to_key)
        return int(self._execute(query).scalar_one())


def _side(from_key: str | None, to_key: str | None) -> tuple[Side | None, str]:
    if from_key is not None and to_key is None:
        return "from", from_key
    if to_key is not None and from_key is None:
        return "to", to_key
    return None, ""
