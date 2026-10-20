"""Structural three-way merge of schema elements (design 6.8 step 2)."""

from __future__ import annotations

from pydantic import JsonValue

from lorebase_core.merge import ConflictKind, load_schema, put_schema
from lorebase_core.merge.schema_merge import merge_document
from lorebase_core.merge.state import MISSING, Value
from lorebase_core.tree import InMemoryNodeStore, tree_apply

from .support import (
    COUNTRY,
    NAME,
    RATING,
    SUPPLIER,
    Edit,
    attempt,
    attribute,
    base_schema,
    fork,
    ident,
    merged,
    replace_attribute,
    replace_kind,
)

ATOMIC = frozenset({"type"})


class Recorder:
    def __init__(self) -> None:
        self.conflicts: list[tuple[str, ...]] = []

    def __call__(self, path: tuple[str, ...], base: Value, ours: Value, theirs: Value) -> Value:
        self.conflicts.append(path)
        return base


def run(base: JsonValue, ours: JsonValue, theirs: JsonValue) -> tuple[Value, list[tuple[str, ...]]]:
    recorder = Recorder()
    result = merge_document(base, ours, theirs, recorder, ATOMIC)
    return result, recorder.conflicts


def test_objects_merge_entry_by_entry() -> None:
    base = {"attributes": {"a": {"x": 1}}, "name": "n"}
    ours = {"attributes": {"a": {"x": 1}, "b": {"x": 2}}, "name": "n"}
    theirs = {"attributes": {"a": {"x": 3}}, "name": "m"}
    result, conflicts = run(base, ours, theirs)
    assert result == {"attributes": {"a": {"x": 3}, "b": {"x": 2}}, "name": "m"}
    assert conflicts == []


def test_scalars_changed_differently_conflict() -> None:
    result, conflicts = run({"name": "n"}, {"name": "o"}, {"name": "t"})
    assert conflicts == [("name",)]
    assert result == {"name": "n"}


def test_lists_conflict_when_both_sides_changed_them() -> None:
    _, conflicts = run({"steps": [1]}, {"steps": [1, 2]}, {"steps": [0, 1]})
    assert conflicts == [("steps",)]
    result, conflicts = run({"steps": [1]}, {"steps": [1, 2]}, {"steps": [1]})
    assert result == {"steps": [1, 2]}
    assert conflicts == []


def test_atomic_properties_merge_as_whole_values() -> None:
    base = {"type": {"name": "decimal", "precision": 10, "scale": 2}}
    ours = {"type": {"name": "decimal", "precision": 12, "scale": 2}}
    theirs = {"type": {"name": "decimal", "precision": 10, "scale": 4}}
    _, conflicts = run(base, ours, theirs)
    assert conflicts == [("type",)]


def test_delete_against_modify_of_an_entry_conflicts() -> None:
    _, conflicts = run({"a": {"x": 1}}, {}, {"a": {"x": 2}})
    assert conflicts == [("a",)]
    result, _ = run({"a": {"x": 1}}, {}, {"a": {"x": 1}, "b": 1})
    assert result == {"b": 1}


def test_absent_and_null_are_the_same() -> None:
    result, conflicts = run(MISSING, {"a": 1}, {"b": 2})  # type: ignore[arg-type]
    assert result == {"a": 1, "b": 2}
    assert conflicts == []


def test_rename_on_one_side_and_attribute_edit_on_the_other_merge_cleanly() -> None:
    schema = base_schema()
    scenario = fork(
        Edit(schema=replace_kind(schema, SUPPLIER, api_name="Vendor")),
        Edit(schema=replace_attribute(schema, SUPPLIER, NAME, description="Legal name")),
    )
    result = merged(scenario.merge())
    kind = scenario.repo.schema(result.root).kinds[SUPPLIER]
    assert kind.api_name == "Vendor"
    assert kind.attributes[NAME].description == "Legal name"


def test_attributes_added_on_both_sides_are_kept() -> None:
    schema = base_schema()
    email, phone = ident("att", 61), ident("att", 62)
    ours = replace_kind(
        schema,
        SUPPLIER,
        attributes={**schema.kinds[SUPPLIER].attributes, email: attribute(email, "email")},
    )
    theirs = replace_kind(
        schema,
        SUPPLIER,
        attributes={**schema.kinds[SUPPLIER].attributes, phone: attribute(phone, "phone")},
    )
    scenario = fork(Edit(schema=ours), Edit(schema=theirs))
    result = merged(scenario.merge())
    assert {email, phone} <= set(scenario.repo.schema(result.root).kinds[SUPPLIER].attributes)


def test_same_change_on_both_sides_is_taken_once() -> None:
    changed = replace_attribute(base_schema(), SUPPLIER, RATING, description="Score")
    scenario = fork(Edit(schema=changed), Edit(schema=changed))
    result = merged(scenario.merge())
    assert result.changes == ()
    assert result.root == scenario.repo.roots[scenario.ours]


def test_attribute_api_name_collision_is_a_naming_conflict() -> None:
    schema = base_schema()
    first, second = ident("att", 71), ident("att", 72)
    attributes = schema.kinds[SUPPLIER].attributes
    scenario = fork(
        Edit(
            schema=replace_kind(
                schema, SUPPLIER, attributes={**attributes, first: attribute(first, "email")}
            )
        ),
        Edit(
            schema=replace_kind(
                schema, SUPPLIER, attributes={**attributes, second: attribute(second, "email")}
            )
        ),
    )
    conflicts = attempt(scenario.merge()).conflicts
    assert [(c.kind, c.key) for c in conflicts] == [(ConflictKind.NAMING, SUPPLIER)]
    assert conflicts[0].attribute in (
        f"attributes.{first}.api_name",
        f"attributes.{second}.api_name",
    )


def test_schema_partition_round_trip() -> None:
    from lorebase_core.merge import InMemoryObjectStore

    store, objects = InMemoryNodeStore(), InMemoryObjectStore()
    schema = replace_attribute(base_schema(), SUPPLIER, COUNTRY, required=True)
    root = tree_apply(store, None, put_schema(objects, schema))
    assert load_schema(store, objects, root) == schema
