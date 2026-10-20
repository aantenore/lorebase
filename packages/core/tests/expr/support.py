"""Shared helpers of the expression engine tests: the supply schema and evaluation inputs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from functools import cache
from pathlib import Path

from pydantic import JsonValue

from lorebase_core.expr import EvaluationContext, MemoryResolver, Principal
from lorebase_core.model import Kind, Record, Schema, load_schema

SUPPLY_SCHEMA = Path(__file__).parents[4] / "examples" / "supply" / "schema.yaml"
NOW = datetime(2026, 10, 21, 9, 30, tzinfo=UTC)
PRINCIPAL = Principal("user:ana", ("risk-analyst", "group:procurement"))


@cache
def supply() -> Schema:
    return load_schema(SUPPLY_SCHEMA.read_text(encoding="utf-8"))


def kind(name: str) -> Kind:
    found = supply().kind_by_api_name(name)
    assert found is not None
    return found


def values(kind_name: str, by_name: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Canonical values keyed by Attribute id from values keyed by api name."""
    owner = kind(kind_name)
    result: dict[str, JsonValue] = {}
    for name, value in by_name.items():
        attribute = owner.attribute_by_api_name(name)
        assert attribute is not None, name
        result[attribute.id] = value
    return result


def record(kind_name: str, key: str, by_name: Mapping[str, JsonValue]) -> Record:
    return Record(kind=kind(kind_name).id, key=key, values=values(kind_name, by_name))


def minter() -> Callable[[], str]:
    counter = iter(range(1, 1000))
    return lambda: f"01KF00000000000000000000{next(counter):02d}"


def context(resolver: MemoryResolver | None = None, **overrides: object) -> EvaluationContext:
    fields: dict[str, object] = {
        "now": NOW,
        "resolver": resolver if resolver is not None else MemoryResolver(),
        "principal": PRINCIPAL,
        "mint_key": minter(),
    }
    fields.update(overrides)
    return EvaluationContext(**fields)  # type: ignore[arg-type]
