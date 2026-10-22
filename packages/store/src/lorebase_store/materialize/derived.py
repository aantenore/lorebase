"""Derived values in the materialized head (design 5.8, 6.6 step 6, AC-07).

`expression` and `aggregate` Derivations compute an Attribute of a Record from its own values
and from the Records one hop away. Their results live in the `derived` column of the
materialized row, keyed by Attribute id: never in the tree, so they take no part in hashing or
merge and are recomputed from merged inputs.

`DerivedValues` evaluates the Derivations of a schema in dependency order (a Derivation that
reads a derived Attribute runs after the one computing it):

- `recompute_all` computes every Record of every Kind with a Derivation (rebuilds, schema
  changes, verification);
- `recompute` computes, after a changeset was applied, the Records it can affect: Records
  written, Records whose Links of a Relation the Derivation reads changed (for a path
  Derivation, a Link of any of its steps), Records linked to written Records or to Records
  whose derived values changed. One Derivation recomputes at most
  `store.materialize.derivation_max_incremental_records` Records this way; beyond that, every
  Record of its Kind.

Evaluation is bounded by `schema.expression_limits`; `now()` is the time of the Revision. A
value that cannot be computed (an evaluation error, a result that does not fit the Attribute)
is absent and counted in `failures`. With `write=False` nothing is written: results are kept in
memory (`expected`) for verification. `preview` evaluates one Derivation on a few Records and
returns the values and the errors without writing, whatever `write` is (candidate Derivations
that are not stored yet).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import JsonValue

from lorebase_core.expr import (
    CompiledDerivation,
    EvaluationContext,
    ExprError,
    ExprErrorCode,
    ExprLimits,
    LinkedRecord,
    LinksRequest,
    MemoryResolver,
    RecordNotLoaded,
    compile_derivation,
    derivation_inputs,
    evaluate_derivation,
    links_read,
    matches_filter,
)
from lorebase_core.expr.values import encode_result, to_runtime
from lorebase_core.model import (
    AggregateDerivation,
    AggregateFunction,
    Attribute,
    Derivation,
    ExpressionDerivation,
    Kind,
    LinkDirection,
    PathRelation,
    Record,
    Relation,
    RelationBacking,
    Schema,
)
from lorebase_core.types import AttributeType
from lorebase_store.bulk import chunks
from lorebase_store.materialize.links import (
    links_of,
    path_affected,
    path_links_of,
    reverse,
    reverse_steps,
)
from lorebase_store.repositories.materialized import MaterializedRepository, RecordRow

logger = logging.getLogger("lorebase.store.derived")

RecordKey = tuple[str, str]


@dataclass(frozen=True, eq=False)
class Hop:
    """A Relation traversed from the Derivation's Kind (`relation.endpoint(direction)`);
    hashed by identity (one object per Derivation step). For a path Derivation, `relation`
    is its virtual Relation and `path` the path."""

    relation: Relation
    direction: LinkDirection
    path: PathRelation | None = None

    @property
    def near_kind(self) -> str:
        return self.relation.endpoint(self.direction)

    @property
    def far_kind(self) -> str:
        return self.relation.far_endpoint(self.direction)


@dataclass(frozen=True)
class DerivationStep:
    """One Derivation ready to evaluate: its target, compiled expressions and the Relations
    whose Links and far Records it reads."""

    derivation: Derivation
    kind: Kind
    attribute: Attribute
    compiled: CompiledDerivation
    hops: tuple[Hop, ...]
    inputs: frozenset[str]


def plan_derivations(schema: Schema, limits: ExprLimits) -> list[DerivationStep]:
    """The `expression` and `aggregate` Derivations of `schema` in dependency order.

    A Derivation that does not compile (only possible for schemas committed with other rules)
    is left out and logged.
    """
    steps: dict[str, DerivationStep] = {}
    for derivation in sorted(schema.derivations.values(), key=lambda d: d.id):
        definition = derivation.definition
        if not isinstance(definition, ExpressionDerivation | AggregateDerivation):
            continue
        kind = schema.kinds.get(definition.kind)
        attribute = None if kind is None else kind.attributes.get(definition.attribute)
        if kind is None or attribute is None:
            continue
        try:
            compiled = compile_derivation(schema, derivation, limits)
        except ExprError as error:
            logger.warning(
                "derivation skipped",
                extra={"derivation": derivation.id, "error": error.describe()},
            )
            continue
        hops: list[Hop] = []
        paths = schema.path_relations()
        if isinstance(definition, AggregateDerivation):
            relation = schema.link_relation(definition.relation)
            if relation is None:
                continue
            hops.append(Hop(relation, definition.direction, paths.get(relation.id)))
        elif compiled.expression is not None:
            hops.extend(
                Hop(f.relation, f.direction, paths.get(f.relation.id))
                for f in links_read(compiled.expression)
            )
        steps[attribute.id] = DerivationStep(
            derivation,
            kind,
            attribute,
            compiled,
            tuple(hops),
            derivation_inputs(schema, derivation, limits),
        )
    ordered: list[DerivationStep] = []
    done: set[str] = set()
    visiting: set[str] = set()

    def visit(attribute_id: str) -> None:
        if attribute_id in done or attribute_id in visiting:
            return  # a cycle is reported by schema validation; keep a stable order
        visiting.add(attribute_id)
        step = steps[attribute_id]
        for dependency in sorted(step.inputs):
            if dependency in steps:
                visit(dependency)
        visiting.discard(attribute_id)
        done.add(attribute_id)
        ordered.append(step)

    for attribute_id in sorted(steps):
        visit(attribute_id)
    return ordered


@dataclass
class AppliedChanges:
    """What a changeset did to the materialized head, as derived values need it.

    `written` and `removed` are Record keys by Kind id; `links` the `(from, to)` keys of
    changed Links by Relation id; `previous` the values of written or removed Records before
    the change (`None` for new Records).
    """

    written: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    removed: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    links: dict[str, set[tuple[str, str]]] = field(default_factory=lambda: defaultdict(set))
    previous: dict[RecordKey, Mapping[str, Any] | None] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PreviewedValue:
    """The value a Derivation computes for one Record: `value` is `None` when there is none,
    `error` says why the evaluation failed."""

    key: str
    title: str | None
    value: JsonValue
    error: ExprError | None = None


@dataclass
class _Loaded:
    """Inputs of one batch: Records of the Derivation's Kind and, per hop, the Links and far
    Records."""

    rows: dict[str, RecordRow]
    links: dict[Hop, dict[str, list[LinkedRecord]]]


class DerivedValues:
    """Evaluates the Derivations of one schema on one branch (see the module docstring)."""

    def __init__(
        self,
        repository: MaterializedRepository,
        schema: Schema,
        *,
        limits: ExprLimits,
        now: datetime,
        batch: int,
        max_incremental: int,
        fanout: int,
        write: bool = True,
    ) -> None:
        self.repository = repository
        self.schema = schema
        self.limits = limits
        self.now = now
        self.batch = batch
        self.fanout = fanout
        self.max_incremental = max_incremental
        self.write = write
        self.steps = plan_derivations(schema, limits)
        self.expected: dict[RecordKey, dict[str, JsonValue]] = {}
        self.failures = 0

    @property
    def kinds(self) -> set[str]:
        """Kinds with at least one Derivation."""
        return {step.kind.id for step in self.steps}

    @property
    def reads_paths(self) -> bool:
        """Whether a Derivation reads a path Derivation (its values follow the fan-out)."""
        return any(hop.path is not None for step in self.steps for hop in step.hops)

    def searchable(self, kind_id: str) -> bool:
        """Whether a derived Attribute of the Kind is in its search vector."""
        return any(s.kind.id == kind_id and s.attribute.searchable for s in self.steps)

    # Entry points -----------------------------------------------------------------------------

    async def recompute_all(self) -> dict[str, set[str]]:
        """Recompute every derived value; returns the keys whose values changed, by Kind."""
        changed: dict[str, set[str]] = defaultdict(set)
        for step in self.steps:
            changed[step.kind.id] |= await self._run(step, None)
        return changed

    async def prune(self) -> None:
        """Drop stored derived values of Attributes that are no longer derived."""
        keep: dict[str, list[str]] = defaultdict(list)
        for step in self.steps:
            keep[step.kind.id].append(step.attribute.id)
        for kind_id, attributes in sorted(keep.items()):
            await self.repository.prune_derived(kind_id, attributes)
        await self.repository.clear_derived(sorted(keep))

    async def recompute_step(self, step: DerivationStep, keys: Iterable[str]) -> set[str]:
        """Recompute one Derivation for the Records `keys` of its Kind; returns the keys
        whose value changed. Running every step in order over every key of its Kind is
        `recompute_all`, in pieces a background job can commit one by one."""
        return await self._run(step, keys)

    async def recompute(self, applied: AppliedChanges) -> dict[str, set[str]]:
        """Recompute the derived values a changeset can affect; returns changed keys by Kind."""
        changed: dict[str, set[str]] = defaultdict(set)
        for step in self.steps:
            kind_id = step.kind.id
            keys = set(applied.written.get(kind_id, ())) | changed[kind_id]
            for hop in step.hops:
                keys |= await self._affected_through(hop, applied, changed)
            keys -= applied.removed.get(kind_id, set())
            if not keys:
                continue
            target = None if len(keys) > self.max_incremental else keys
            changed[kind_id] |= await self._run(step, target)
        return changed

    async def preview(
        self, attribute_id: str, *, keys: Sequence[str] | None, limit: int
    ) -> list[PreviewedValue]:
        """The values the Derivation of `attribute_id` computes for `keys` (the first `limit`
        Records of its Kind by key when `None`), from the stored and derived values of the
        branch. Nothing is written; Records that do not exist are left out."""
        step = next((s for s in self.steps if s.attribute.id == attribute_id), None)
        if step is None:
            raise ExprError(ExprErrorCode.EVALUATION, "the Derivation cannot be evaluated")
        if keys is None:
            rows = await self.repository.record_page(step.kind.id, after=None, limit=limit)
            batch = [row.key for row in rows]
        else:
            batch = list(dict.fromkeys(keys))[:limit]
        loaded = await self._load(step, batch)
        previewed: list[PreviewedValue] = []
        for key in sorted(loaded.rows):
            row = loaded.rows[key]
            try:
                previewed.append(
                    PreviewedValue(key, row.title, await self._compute(step, row, loaded))
                )
            except ExprError as error:
                previewed.append(PreviewedValue(key, row.title, None, error))
        return previewed

    # Affected Records ---------------------------------------------------------------------------

    async def _affected_through(
        self, hop: Hop, applied: AppliedChanges, changed: Mapping[str, set[str]]
    ) -> set[str]:
        """Keys of the hop's near Kind whose Links or far Records changed. For a path, the
        Records whose pairs a change of any step can change (`path_affected`)."""
        relation = hop.relation
        forward = hop.direction is LinkDirection.FORWARD
        if hop.path is not None:
            steps = hop.path.steps if forward else reverse_steps(hop.path.steps)
            keys = await path_affected(
                self.repository, steps, applied, fanout=self.fanout, inverse=not forward
            )
        else:
            keys = {f if forward else t for f, t in applied.links.get(relation.id, ())}
        far = hop.far_kind
        far_keys = (
            set(applied.written.get(far, ()))
            | set(applied.removed.get(far, ()))
            | set(changed.get(far, ()))
        )
        if relation.backing is RelationBacking.FOREIGN_KEY:
            keys |= self._foreign_key_targets(hop, applied)
        if far_keys:
            back = Hop(relation, reverse(hop.direction), hop.path)
            for near_keys in (await self._links(back, sorted(far_keys))).values():
                keys.update(far_key for far_key, _ in near_keys)
        return keys

    def _foreign_key_targets(self, hop: Hop, applied: AppliedChanges) -> set[str]:
        """Targets, before and after the change, of written or removed `from` Records of a
        foreign-key Relation read from its `to` end."""
        relation = hop.relation
        attribute = relation.foreign_key_attribute
        if hop.direction is LinkDirection.FORWARD or attribute is None:
            return set()  # the near Records hold the foreign key: they were written
        targets: set[str] = set()
        source = relation.from_kind
        for key in set(applied.written.get(source, ())) | set(applied.removed.get(source, ())):
            before = applied.previous.get((source, key))
            if before is not None and isinstance(before.get(attribute), str):
                targets.add(before[attribute])
        return targets

    # Loading ----------------------------------------------------------------------------------

    async def _links(self, hop: Hop, keys: Sequence[str]) -> dict[str, list[tuple[str, Any]]]:
        """`near key -> [(far key, link values)]` of the hop for `keys`."""
        if hop.path is not None:
            return await path_links_of(self.repository, hop.path, hop.direction, keys, self.fanout)
        return await links_of(self.repository, hop.relation, hop.direction, keys)

    def _inputs(self, kind_id: str, row: RecordRow) -> dict[str, Any]:
        """Stored and derived values of a Record; derived ones from `expected` when not
        writing (verification compares stored values with recomputed ones)."""
        if self.write:
            return row.inputs
        return {**row.values, **self.expected.get((kind_id, row.key), {})}

    async def _linked(self, hop: Hop, keys: Sequence[str]) -> dict[str, list[LinkedRecord]]:
        links = await self._links(hop, keys)
        far_keys = sorted({far for items in links.values() for far, _ in items})
        rows = await self.repository.records(hop.far_kind, far_keys) if far_keys else {}
        return {
            near: [
                LinkedRecord(far, self._inputs(hop.far_kind, rows[far]), values)
                for far, values in items
                if far in rows
            ]
            for near, items in links.items()
        }

    async def _load(self, step: DerivationStep, keys: Sequence[str]) -> _Loaded:
        rows = await self.repository.records(step.kind.id, keys)
        present = sorted(rows)
        links = {hop: await self._linked(hop, present) for hop in step.hops}
        return _Loaded(rows, links)

    # Evaluation -------------------------------------------------------------------------------

    async def _run(self, step: DerivationStep, keys: Iterable[str] | None) -> set[str]:
        """Recompute one Derivation for `keys` (every Record of its Kind when `None`)."""
        changed: set[str] = set()
        async for batch in self._batches(step.kind.id, keys):
            loaded = await self._load(step, batch)
            updates: list[tuple[str, JsonValue]] = []
            for key in batch:
                row = loaded.rows.get(key)
                if row is None:
                    continue
                value = await self._value(step, row, loaded)
                current = self._inputs(step.kind.id, row).get(step.attribute.id)
                if not self.write:
                    record = self.expected.setdefault((step.kind.id, key), {})
                    if value is not None:
                        record[step.attribute.id] = value
                if value != current:
                    updates.append((key, value))
                    changed.add(key)
            if self.write and updates:
                await self.repository.set_derived(step.kind.id, step.attribute.id, updates)
        return changed

    async def _batches(self, kind_id: str, keys: Iterable[str] | None) -> AsyncIterator[list[str]]:
        if keys is not None:
            for chunk in chunks(sorted(keys), self.batch):
                yield list(chunk)
            return
        async for rows in self.repository.stream_records(kind_id, batch=self.batch):
            yield [row.key for row in rows]

    async def _value(self, step: DerivationStep, row: RecordRow, loaded: _Loaded) -> JsonValue:
        try:
            return await self._compute(step, row, loaded)
        except ExprError as error:
            self.failures += 1
            logger.debug(
                "derived value not computed",
                extra={"derivation": step.derivation.id, "key": row.key, "error": str(error)},
            )
            return None

    async def _compute(self, step: DerivationStep, row: RecordRow, loaded: _Loaded) -> JsonValue:
        """The value of one Record; raises `ExprError` when it cannot be computed."""
        resolver = MemoryResolver()
        for hop, linked in loaded.links.items():
            resolver.links[(hop.relation.id, hop.direction, row.key)] = linked.get(row.key, [])
        context = EvaluationContext(now=self.now, resolver=resolver, limits=self.limits)
        record = Record(kind=step.kind.id, key=row.key, values=self._inputs(step.kind.id, row))
        compiled = step.compiled
        definition = step.derivation.definition
        if isinstance(definition, ExpressionDerivation):
            return await self._loading(
                lambda: evaluate_derivation(compiled, record, context), resolver
            )
        assert isinstance(definition, AggregateDerivation)
        hop = step.hops[0]
        far = self.schema.kinds[hop.far_kind]
        members = []
        for item in loaded.links[hop].get(row.key, []):
            candidate = Record(kind=far.id, key=item.key, values=dict(item.values))

            def passes(record: Record = candidate) -> bool:
                return matches_filter(compiled, record, context)

            if await self._loading(passes, resolver):
                members.append(item)
        return aggregate(step.attribute, definition, far, members)

    async def _loading[T](self, compute: Callable[[], T], resolver: MemoryResolver) -> T:
        """Run an evaluation, loading the Link lists it asks for (filters reading Links of
        aggregated Records), then run it again."""
        for _ in range(self.limits.max_record_lookups + 1):
            try:
                return compute()
            except RecordNotLoaded as missing:
                request = missing.request
                if not isinstance(request, LinksRequest):
                    raise ExprError(
                        ExprErrorCode.EVALUATION, "Records cannot be read by a Derivation"
                    ) from None
                relation = self.schema.link_relation(request.relation)
                if relation is None:
                    raise ExprError(
                        ExprErrorCode.EVALUATION, f"unknown Relation {request.relation}"
                    ) from None
                path = self.schema.path_relations().get(relation.id)
                hop = Hop(relation, request.direction, path)
                linked = await self._linked(hop, [request.key])
                resolver.links[(request.relation, request.direction, request.key)] = linked.get(
                    request.key, []
                )
        return compute()


def aggregate(
    target: Attribute,
    definition: AggregateDerivation,
    far: Kind,
    members: Sequence[LinkedRecord],
) -> JsonValue:
    """The canonical value of an aggregate over the far Records that passed the filter.

    `count` counts them; `sum` adds the source values (0 when none); `min`, `max` and `avg`
    have no value without members. Decimal results are rounded to the target's scale.
    """
    if definition.function is AggregateFunction.COUNT:
        return encode_result(target.type, len(members))
    assert definition.source_attribute is not None
    source = far.attributes[definition.source_attribute]
    values: list[Any] = [
        value
        for member in members
        if (value := to_runtime(source.type, member.values.get(source.id))) is not None
    ]
    result: object
    if definition.function is AggregateFunction.SUM:
        start: Any = Decimal(0) if source.type.name is AttributeType.DECIMAL else 0
        result = sum(values, start)
    elif not values:
        return None
    elif definition.function is AggregateFunction.MIN:
        result = min(values)
    elif definition.function is AggregateFunction.MAX:
        result = max(values)
    else:
        total = sum(Decimal(str(v)) for v in values)
        result = total / len(values)
    if target.type.name is AttributeType.DECIMAL and isinstance(result, int | float | Decimal):
        assert target.type.scale is not None
        result = Decimal(str(result)).quantize(Decimal(1).scaleb(-target.type.scale))
    elif target.type.name is AttributeType.INTEGER and isinstance(result, Decimal):
        if result != result.to_integral_value():
            raise ExprError(ExprErrorCode.VALUE_INVALID, f"{result} is not an integer")
        result = int(result)
    return encode_result(target.type, result)
