"""Data merge (design 6.8 step 3): Records and Links under the merged schema.

Each side's version of a changed element is first lifted into the merged schema: values of
Attributes the merged schema no longer has are dropped (orphaned values when that side wrote
them) and values of Attributes whose type changed are cast (type coercion Conflicts when the
cast fails). The lifted versions then merge Attribute by Attribute against the lifted base.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from pydantic import JsonValue

from lorebase_core.canonical import ObjectType
from lorebase_core.merge.conflicts import ConflictKind, ResolutionChoice, ResolutionError, Side
from lorebase_core.merge.objects import data_object_type, partition_element
from lorebase_core.merge.schema_merge import SchemaMerge, pick
from lorebase_core.merge.state import (
    MISSING,
    SIDES,
    Context,
    Payload,
    Stage,
    Value,
    as_json,
    same,
)
from lorebase_core.model import Attribute, Kind, Relation, Schema
from lorebase_core.tree import PARTITION_SCHEMA, DiffEntry, relation_partition, tree_get
from lorebase_core.types import CastError, CastRule, cast, cast_rule

__all__ = ["DataMerge", "Origin", "SideChanges", "merge_data", "side_changes"]

#: Changed data elements of one side against the base: ``(partition, key) -> (base, side)``.
SideChanges = dict[tuple[str, str], tuple[bytes | None, bytes | None]]


@dataclass
class Origin:
    """Where the merged version of an element comes from, for provenance."""

    whole: Side | None = None
    attributes: dict[str, Side] = field(default_factory=dict)


@dataclass
class DataMerge:
    """Merged states of every data element changed on either side."""

    #: ``(partition, key) -> payload``, ``None`` when the element is deleted.
    states: dict[tuple[str, str], Payload | None] = field(default_factory=dict)
    #: Ours' object hash of each element in ``states``.
    ours: dict[tuple[str, str], bytes | None] = field(default_factory=dict)
    origins: dict[tuple[str, str], Origin] = field(default_factory=dict)

    def present(self, partition: str | None = None) -> Iterable[tuple[tuple[str, str], Payload]]:
        """Elements present after the merge, optionally of one partition, in key order."""
        for location in sorted(self.states):
            state = self.states[location]
            if state is not None and (partition is None or location[0] == partition):
                yield location, state


def side_changes(entries: Iterable[DiffEntry]) -> SideChanges:
    """Data changes of a side from its diff against the base (the ``schema`` is left out)."""
    return {
        (e.partition, e.key): (e.hash_a, e.hash_b)
        for e in entries
        if e.partition != PARTITION_SCHEMA
    }


def merge_data(ctx: Context, schema: SchemaMerge, changes: Mapping[Side, SideChanges]) -> DataMerge:
    """Merge every data element changed on either side."""
    result = DataMerge()
    by_partition: dict[str, dict[str, bytes | None]] = defaultdict(dict)
    for side in SIDES:
        for (partition, key), (base_hash, _) in changes[side].items():
            by_partition[partition][key] = base_hash
    for partition in sorted(by_partition):
        merger = _PartitionMerge(ctx, schema, changes, partition, result)
        merger.run(by_partition[partition])
    _apply_directives(ctx, result)
    if ctx.policy.apply_defaults and not ctx.virtual:
        _apply_defaults(ctx, schema, result)
    return result


class _PartitionMerge:
    def __init__(
        self,
        ctx: Context,
        schema: SchemaMerge,
        changes: Mapping[Side, SideChanges],
        partition: str,
        result: DataMerge,
    ) -> None:
        self.ctx = ctx
        self.schema = schema
        self.changes = changes
        self.partition = partition
        self.result = result
        element = partition_element(partition)
        self.element_id = element
        self.is_link = element is not None and partition == relation_partition(element)
        self.merged = self._element(schema.schema, element)
        self.sides: dict[Side, Kind | Relation | None] = {
            side: self._element(ctx.schemas[side], element)
            for side in (Side.BASE, Side.OURS, Side.THEIRS)
        }
        merged_hash = None if element is None else schema.hashes.get(element)
        self.lift_needed = {
            side: element is not None
            and (
                ctx.schema_hashes[side].get(element) != merged_hash
                or any(
                    s is side and p == partition for s, p, _ in ctx.directives.ignored_attributes
                )
            )
            for side in (Side.BASE, Side.OURS, Side.THEIRS)
        }
        self.failures: set[tuple[Side, str]] = set()
        self.bases: Mapping[str, bytes | None] = {}

    def _element(self, schema: Schema | None, element: str | None) -> Kind | Relation | None:
        if element is None or schema is None:
            return None
        if self.is_link:
            return schema.relations.get(element)
        return schema.kinds.get(element)

    # Hashes ---------------------------------------------------------------------

    def side_hash(self, side: Side, key: str, base: bytes | None) -> bytes | None:
        if (side, self.partition) in self.ctx.directives.ignored_partitions:
            return base
        change = self.changes[side].get((self.partition, key))
        return base if change is None else change[1]

    # Entry point ----------------------------------------------------------------

    def run(self, keys: Mapping[str, bytes | None]) -> None:
        self.bases = keys
        if self.element_id is None:
            for key, base in sorted(keys.items()):
                self.store(key, self.ctx.document(self.side_hash(Side.THEIRS, key, base)))
            return
        if self.merged is None:
            self.orphans(keys)
            return
        for key, base in sorted(keys.items()):
            self.merge_key(key, base)

    def store(self, key: str, state: Payload | None, origin: Origin | None = None) -> None:
        location = (self.partition, key)
        ours = self.changes[Side.OURS].get(location)
        self.result.states[location] = state
        self.result.ours[location] = self.bases[key] if ours is None else ours[1]
        self.result.origins[location] = origin or Origin()

    # Orphaned data --------------------------------------------------------------

    def orphans(self, keys: Mapping[str, bytes | None]) -> None:
        """The Kind or Relation is gone from the merged schema: its data is dropped."""
        assert self.element_id is not None
        for side in SIDES:
            if self.sides[side] is None:
                continue
            members = [key for key, base in keys.items() if self.side_hash(side, key, base) != base]
            if members and self.partition not in self.ctx.blocked:
                group = self.ctx.group(
                    ConflictKind.ORPHANED_DATA,
                    self.partition,
                    None,
                    None,
                    stage=Stage.DECISION,
                    element=self.element_id,
                    data_side=side,
                    message=(
                        f"{self.element_id} was removed on one side; {len(members)} element(s) "
                        f"of it changed on {side.value}"
                    ),
                )
                group.members.update(dict.fromkeys(members))
        for key in sorted(keys):
            self.store(key, None)

    # Elements -------------------------------------------------------------------

    def merge_key(self, key: str, base_hash: bytes | None) -> None:
        ours_hash = self.side_hash(Side.OURS, key, base_hash)
        theirs_hash = self.side_hash(Side.THEIRS, key, base_hash)
        changed_ours = ours_hash != base_hash
        changed_theirs = theirs_hash != base_hash
        if ours_hash == theirs_hash and not self.lift_needed[Side.OURS]:
            self.store(key, self.ctx.document(ours_hash))
            return
        if changed_theirs and not changed_ours and not self.lift_needed[Side.THEIRS]:
            self.store(key, self.ctx.document(theirs_hash), Origin(whole=Side.THEIRS))
            return
        if changed_ours and not changed_theirs and not self.lift_needed[Side.OURS]:
            self.store(key, self.ctx.document(ours_hash), Origin(whole=Side.OURS))
            return
        hashes = (base_hash, ours_hash, theirs_hash)
        payloads = [self.ctx.document(h) for h in hashes]
        base_values = _values(payloads[0])
        lifted_base = self.lift(Side.BASE, key, base_values, base_values)
        lifted: dict[Side, dict[str, JsonValue] | None] = {}
        for side, payload in zip(SIDES, payloads[1:], strict=True):
            lifted[side] = (
                None if payload is None else self.lift(side, key, _values(payload), base_values)
            )
            for failed_side, attribute in self.failures:
                values = lifted[side]
                if failed_side is side and values is not None:
                    values.pop(attribute, None)
                    if attribute in lifted_base:
                        values[attribute] = lifted_base[attribute]
            self.failures.clear()
        template = next((p for p in (payloads[1], payloads[2], payloads[0]) if p is not None), None)
        if template is None:
            self.store(key, None)
            return
        if changed_ours and changed_theirs:
            if payloads[1] is None and payloads[2] is None:
                self.store(key, None)
            elif payloads[0] is not None and (payloads[1] is None or payloads[2] is None):
                self.delete_modify(key, hashes, template, lifted_base, lifted)
            else:
                values, origin = self.merge_values(
                    key, hashes, lifted_base, lifted[Side.OURS] or {}, lifted[Side.THEIRS] or {}
                )
                self.store(key, _with_values(template, values), origin)
            return
        side = Side.THEIRS if changed_theirs else Side.OURS
        values_of_side = lifted[side]
        state = None if values_of_side is None else _with_values(template, values_of_side)
        self.store(key, state, Origin(whole=side) if changed_theirs or changed_ours else None)

    def lift(
        self, side: Side, key: str, values: dict[str, JsonValue], base: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        """Values of one side expressed in the merged schema."""
        merged = self.merged
        assert merged is not None
        side_element = self.sides[side]
        side_attributes = merged.attributes if side_element is None else side_element.attributes
        values = self.unignored(side, values, base)
        lifted: dict[str, JsonValue] = {}
        for attribute_id, value in values.items():
            wrote = side is not Side.BASE and not same(value, base.get(attribute_id, MISSING))
            target = merged.attributes.get(attribute_id)
            if target is None:
                if wrote and attribute_id in side_attributes:
                    self.problem(ConflictKind.ORPHANED_VALUE, side, attribute_id, key, value)
                continue
            source = side_attributes.get(attribute_id)
            if source is None or source.type == target.type:
                lifted[attribute_id] = value
                continue
            converted = self.convert(source, target, value)
            if converted is MISSING:
                if wrote:
                    self.problem(ConflictKind.TYPE_COERCION, side, attribute_id, key, value)
                    self.failures.add((side, attribute_id))
                continue
            lifted[attribute_id] = as_json(converted)
        return lifted

    def unignored(
        self, side: Side, values: dict[str, JsonValue], base: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        """``values`` with base values for the Attributes whose ``side`` changes are ignored."""
        result = values
        for ignored_side, partition, attribute_id in self.ctx.directives.ignored_attributes:
            if ignored_side is not side or partition != self.partition:
                continue
            result = dict(result)
            if attribute_id in base:
                result[attribute_id] = base[attribute_id]
            else:
                result.pop(attribute_id, None)
        return result

    def convert(self, source: Attribute, target: Attribute, value: JsonValue) -> Value:
        rule = cast_rule(source.type, target.type)
        allowed = rule is CastRule.SAFE or (
            rule is CastRule.CHECKED and self.ctx.policy.allow_checked_casts
        )
        if not allowed:
            return MISSING
        try:
            return cast(source.type, target.type, value)
        except CastError:
            return MISSING

    def problem(
        self, kind: ConflictKind, side: Side, attribute_id: str, key: str, value: JsonValue
    ) -> None:
        if self.partition in self.ctx.blocked:
            return
        assert self.element_id is not None
        label = "removed" if kind is ConflictKind.ORPHANED_VALUE else "retyped"
        group = self.ctx.group(
            kind,
            self.partition,
            None,
            attribute_id,
            stage=Stage.DECISION,
            element=self.element_id,
            data_side=side,
            message=f"{attribute_id} was {label} on one side and written on {side.value}",
        )
        group.members[key] = value

    def delete_modify(
        self,
        key: str,
        hashes: tuple[bytes | None, bytes | None, bytes | None],
        template: Payload,
        base: dict[str, JsonValue],
        lifted: Mapping[Side, dict[str, JsonValue] | None],
    ) -> None:
        versions: list[Value] = [
            base,
            *(MISSING if lifted[s] is None else lifted[s] for s in SIDES),
        ]
        conflict = self.ctx.conflict(
            ConflictKind.DELETE_MODIFY,
            self.partition,
            key,
            None,
            (base, lifted[Side.OURS], lifted[Side.THEIRS]),
            hashes,
            message=f"{key} was deleted on one side and modified on the other",
        )
        resolution = (
            None
            if self.partition in self.ctx.frozen
            else self.ctx.decide(conflict, self.element_id, None)
        )
        if resolution is None:
            if self.partition not in self.ctx.frozen:
                self.ctx.report(conflict)
            self.ctx.open_keys.add((self.partition, key))
            self.store(key, _with_values(template, base))
            return
        chosen = pick(resolution, versions[0], versions[1], versions[2])
        if chosen is MISSING or chosen is None:
            self.store(key, None, _origin_of(resolution.choice))
            return
        if not isinstance(chosen, dict):
            raise ResolutionError(f"custom value of {conflict.fingerprint} must be a value map")
        self.store(key, _with_values(template, chosen), _origin_of(resolution.choice))

    def merge_values(
        self,
        key: str,
        hashes: tuple[bytes | None, bytes | None, bytes | None],
        base: dict[str, JsonValue],
        ours: dict[str, JsonValue],
        theirs: dict[str, JsonValue],
    ) -> tuple[dict[str, JsonValue], Origin]:
        """Attribute-level three-way merge; arrays compare as whole values."""
        kind = ConflictKind.ADD_ADD if hashes[0] is None else ConflictKind.ATTRIBUTE_VALUE
        merged: dict[str, JsonValue] = {}
        origin = Origin()
        for attribute_id in sorted(base.keys() | ours.keys() | theirs.keys()):
            b = base.get(attribute_id, MISSING)
            o = ours.get(attribute_id, MISSING)
            t = theirs.get(attribute_id, MISSING)
            value: Value
            if same(o, t):
                value = o
            elif same(o, b):
                value = t
                origin.attributes[attribute_id] = Side.THEIRS
            elif same(t, b):
                value = o
                origin.attributes[attribute_id] = Side.OURS
            else:
                value = self.value_conflict(kind, key, attribute_id, hashes, (b, o, t), origin)
            if value is not MISSING and value is not None:
                merged[attribute_id] = value
        return merged, origin

    def value_conflict(
        self,
        kind: ConflictKind,
        key: str,
        attribute_id: str,
        hashes: tuple[bytes | None, bytes | None, bytes | None],
        values: tuple[Value, Value, Value],
        origin: Origin,
    ) -> Value:
        b, o, t = values
        conflict = self.ctx.conflict(
            kind,
            self.partition,
            key,
            attribute_id,
            (as_json(b), as_json(o), as_json(t)),
            hashes,
            message=f"{attribute_id} of {key} changed differently on both sides",
        )
        if self.partition in self.ctx.frozen:
            self.ctx.open_keys.add((self.partition, key))
            return b
        resolution = self.ctx.decide(conflict, self.element_id, attribute_id)
        if resolution is None:
            self.ctx.report(conflict)
            self.ctx.open_keys.add((self.partition, key))
            return b
        side = _origin_of(resolution.choice).whole
        if side is not None:
            origin.attributes[attribute_id] = side
        return pick(resolution, b, o, t)


def _origin_of(choice: ResolutionChoice) -> Origin:
    if choice is ResolutionChoice.THEIRS:
        return Origin(whole=Side.THEIRS)
    if choice is ResolutionChoice.OURS:
        return Origin(whole=Side.OURS)
    return Origin()


def _values(payload: Payload | None) -> dict[str, JsonValue]:
    if payload is None:
        return {}
    values = payload.get("values")
    return dict(values) if isinstance(values, dict) else {}


def _with_values(template: Payload, values: Mapping[str, JsonValue]) -> Payload:
    return {**template, "values": {k: v for k, v in values.items() if v is not None}}


def _apply_directives(ctx: Context, result: DataMerge) -> None:
    """Data overrides from resolutions: whole states, then single values."""
    directives = ctx.directives
    for location, state in sorted(directives.states.items(), key=lambda i: i[0]):
        _ensure(ctx, result, location)
        result.states[location] = state
        result.origins[location] = Origin()
    for (partition, key, attribute_id), value in sorted(
        directives.values.items(), key=lambda i: i[0]
    ):
        location = (partition, key)
        _ensure(ctx, result, location)
        state = result.states[location]
        if state is None:
            continue
        values = _values(state)
        if value is MISSING or value is None:
            values.pop(attribute_id, None)
        else:
            values[attribute_id] = value
        result.states[location] = _with_values(state, values)
        result.origins[location].attributes.pop(attribute_id, None)


def _ensure(ctx: Context, result: DataMerge, location: tuple[str, str]) -> None:
    """Bring an element not changed on either side into the merge set (ours' version)."""
    if location in result.states:
        return
    ours_root = ctx.roots[Side.OURS]
    digest = tree_get(ctx.store, ours_root, location[0], location[1])
    result.states[location] = ctx.document(digest)
    result.ours[location] = digest
    result.origins[location] = Origin()


def _apply_defaults(ctx: Context, schema: SchemaMerge, result: DataMerge) -> None:
    """Missing values of required Attributes take their default (recorded as resolutions)."""
    applied: dict[tuple[str, str], list[str]] = defaultdict(list)
    for (partition, key), state in list(result.present()):
        element = partition_element(partition)
        if element is None or (partition, key) in ctx.open_keys:
            continue
        owner: Kind | Relation | None
        if data_object_type(partition) is ObjectType.LINK:
            owner = schema.schema.relations.get(element)
        else:
            owner = schema.schema.kinds.get(element)
        if owner is None:
            continue
        values = _values(state)
        changed = False
        for attribute in owner.attributes.values():
            if (
                attribute.required
                and not attribute.is_derived
                and attribute.default is not None
                and values.get(attribute.id) is None
            ):
                values[attribute.id] = attribute.default
                applied[(partition, attribute.id)].append(key)
                changed = True
        if changed:
            result.states[(partition, key)] = _with_values(state, values)
    for (partition, attribute_id), keys in sorted(applied.items()):
        element = partition_element(partition)
        conflict = ctx.conflict(
            ConflictKind.MISSING_VALUE,
            partition,
            None,
            attribute_id,
            ctx.element_values(element, attribute_id),
            ctx.element_hashes(element),
            members=keys,
        )
        ctx.record_auto(conflict, ResolutionChoice.CUSTOM, "policy:default")
