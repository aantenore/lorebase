"""The `supply` example in a scratch database, for the Scribe integration tests."""

from __future__ import annotations

import csv
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.model import Kind, Record, Schema
from lorebase_core.model.source import MergePolicy as SourcePolicy
from lorebase_core.model.yaml_schema import load_schema
from lorebase_core.revision import Author, Origin
from lorebase_core.types import AttributeType, encode_value
from lorebase_core.validate import DataChange, LinkUpsert, RecordUpsert
from lorebase_store.approval_rules import ApprovalRuleSettings
from lorebase_store.commit import SchemaUpsert, commit, create_domain
from lorebase_store.runtime import StoreRuntime

EXAMPLE = Path(__file__).resolve().parents[4] / "examples" / "supply"
DOMAIN = "supply"
AUTO_MERGE = ApprovalRuleSettings(required_approvals=0)
"""Approval rules under which automatic merges reach the protected `main`
(`keeper.proposals.required_approvals: 0`); the default rules hold them for review."""
AUTHOR = Author(principal="usr_loader")


class Clock:
    """Strictly increasing clock starting now (merge bases compare commit times)."""

    def __init__(self) -> None:
        self._now = datetime.now(UTC)

    def __call__(self) -> datetime:
        self._now += timedelta(milliseconds=10)
        return self._now


def supply_schema(policies: dict[str, SourcePolicy] | None = None) -> Schema:
    """The example schema without Operations and expression Derivations (commits do not
    enable expressions); `policies` overrides Source merge policies by api name."""
    schema = load_schema((EXAMPLE / "schema.yaml").read_text())
    dropped = {d.id for d in schema.derivations.values() if d.definition.variant == "expression"}
    kinds = {
        kind_id: kind.model_copy(
            update={
                "attributes": {
                    a.id: a for a in kind.attributes.values() if a.derivation_id not in dropped
                }
            }
        )
        for kind_id, kind in schema.kinds.items()
    }
    derivations = {k: d for k, d in schema.derivations.items() if k not in dropped}
    sources = dict(schema.sources)
    for name, policy in (policies or {}).items():
        source = schema.source_by_api_name(name)
        assert source is not None
        sources[source.id] = source.model_copy(
            update={
                "target_branch_policy": source.target_branch_policy.model_copy(
                    update={"merge_policy": policy}
                )
            }
        )
    return schema.model_copy(
        update={
            "kinds": kinds,
            "derivations": derivations,
            "operations": {},
            "sources": sources,
        }
    )


def rows(name: str) -> list[dict[str, str]]:
    with (EXAMPLE / "data" / name).open(newline="") as handle:
        return list(csv.DictReader(handle))


def record_change(kind: Kind, row: dict[str, str]) -> RecordUpsert:
    values: dict[str, Any] = {}
    for name, raw in row.items():
        attribute = kind.attribute_by_api_name(name)
        if attribute is None or raw == "":
            continue
        if attribute.type.name is AttributeType.BOOLEAN:
            values[attribute.id] = raw.strip().lower() == "true"
        else:
            values[attribute.id] = encode_value(attribute.type, raw)
    key = str(values[kind.primary_key[0]])
    return RecordUpsert(record=Record(kind=kind.id, key=key, values=values))


def kind_changes(schema: Schema, kind_name: str, file: str) -> list[DataChange]:
    kind = schema.kind_by_api_name(kind_name)
    assert kind is not None
    return [record_change(kind, row) for row in rows(file)]


async def load_domain(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Callable[[], datetime],
    schema: Schema,
    *,
    kinds: dict[str, str] | None = None,
) -> None:
    """Create the Domain with `schema` and the rows of `kinds` (Kind name -> CSV file)."""
    async with sessions() as session:
        await create_domain(session, schema.domain, author=AUTHOR, runtime=runtime, clock=clock)
        await commit(
            session,
            DOMAIN,
            "main",
            [],
            schema_changes=[SchemaUpsert(e) for e in schema.elements()],  # type: ignore[arg-type]
            author=AUTHOR,
            message="Load the supply schema",
            origin=Origin(type="import"),
            runtime=runtime,
            clock=clock,
        )
        changes: list[DataChange] = []
        for kind_name, file in (kinds or {}).items():
            changes.extend(kind_changes(schema, kind_name, file))
        if changes:
            await commit(
                session,
                DOMAIN,
                "main",
                changes,
                author=AUTHOR,
                message="Import example data",
                origin=Origin(type="import"),
                runtime=runtime,
                clock=clock,
            )


async def commit_on(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Callable[[], datetime],
    changes: list[DataChange | LinkUpsert],
    *,
    branch: str = "main",
) -> None:
    async with sessions() as session:
        await commit(
            session,
            DOMAIN,
            branch,
            changes,
            author=Author(principal="usr_editor"),
            message="Manual edit",
            origin=Origin(type="operation"),
            runtime=runtime,
            clock=clock,
        )


async def scalar(sessions: async_sessionmaker[AsyncSession], sql: str, **params: Any) -> Any:
    async with sessions() as session, session.begin():
        await session.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
        return (await session.execute(text(sql), params)).scalar()


async def fetch(sessions: async_sessionmaker[AsyncSession], sql: str, **params: Any) -> list[Any]:
    async with sessions() as session, session.begin():
        await session.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
        return list((await session.execute(text(sql), params)).all())
