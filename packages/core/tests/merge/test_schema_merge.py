"""Structural three-way merge of schema elements (design 6.8 step 2)."""

from __future__ import annotations

from pydantic import JsonValue

from lorebase_core.merge.schema_merge import merge_document
from lorebase_core.merge.state import MISSING, Value

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
