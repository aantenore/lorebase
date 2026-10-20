"""Derived values in the materialized head: computed on import, kept current on Link changes,
equal after a rebuild, readable and queryable through the API."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Link
from lorebase_core.revision import Origin
from lorebase_core.validate import LinkDelete, LinkUpsert
from lorebase_store.commit import SchemaUpsert, commit
from lorebase_store.materialize import DivergenceReason, rebuild, verify
from lorebase_store.repositories.scope import scope_to_domain
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import StoreSettings

from .read_fixtures import Seeded, keeper, owner_session, seeded_database
from .supply import AUTHOR, DOMAIN, Clock, commit_changes, load_supply, rows

pytestmark = pytest.mark.integration

BASE = f"/api/v1/domains/{DOMAIN}"
DERIVED = (
    "SELECT key, derived FROM mat_records WHERE domain_id = :d AND branch = 'main' "
    "AND kind_id = :k ORDER BY key"
)


def runtime() -> StoreRuntime:
    return StoreRuntime.from_settings(StoreSettings())


def supplier_counts() -> dict[str, int]:
    counts = {row["part_number"]: 0 for row in rows("part.csv")}
    for row in rows("supplied_by.csv"):
        counts[row["from_key"]] += 1
    return counts


async def seed(
    session: AsyncSession, store: StoreRuntime, clock: Clock, owner_url: str, app_url: str
) -> Seeded:
    schema, _ = await load_supply(session, store, clock)
    return Seeded(owner_url=owner_url, app_url=app_url, schema=schema)


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


class Ids:
    def __init__(self, seeded: Seeded) -> None:
        schema = seeded.schema
        part = schema.kind_by_api_name("Part")
        found = schema.relation_by_api_name("supplied_by")
        assert part is not None
        assert found is not None
        self.schema = schema
        self.part = part.id
        self.count = part.attribute_by_api_name("supplier_count").id  # type: ignore[union-attr]
        self.single = part.attribute_by_api_name("single_sourced").id  # type: ignore[union-attr]
        self.supplied_by = found[0]


async def derived_of(session: AsyncSession, kind_id: str) -> dict[str, dict[str, Any]]:
    async with session.begin():
        await scope_to_domain(session, DOMAIN)
        found = await session.execute(text(DERIVED), {"d": DOMAIN, "k": kind_id})
        return {key: dict(derived) for key, derived in found}


async def test_derived_values_follow_imports_and_link_changes(seeded: Seeded) -> None:
    ids = Ids(seeded)
    expected = supplier_counts()
    clock = Clock()
    async with owner_session(seeded.owner_url) as session:
        derived = await derived_of(session, ids.part)
        assert {key: int(d[ids.count]) for key, d in derived.items()} == expected
        assert {key: d[ids.single] for key, d in derived.items()} == {
            key: count == 1 for key, count in expected.items()
        }

        link = Link(relation=ids.supplied_by.id, from_key="P-10001", to_key="S-1007")
        await commit_changes(session, runtime(), clock, [LinkUpsert(link=link)], message="Link")
        after = (await derived_of(session, ids.part))["P-10001"]
        assert after == {ids.count: str(expected["P-10001"] + 1), ids.single: False}
        await commit_changes(
            session,
            runtime(),
            clock,
            [LinkDelete(relation=ids.supplied_by.id, from_key="P-10001", to_key="S-1007")],
            message="Unlink",
        )
        assert (await derived_of(session, ids.part))["P-10001"][ids.single] is True

        incremental = await derived_of(session, ids.part)
        async with session.begin():
            await rebuild(session, DOMAIN, "main", runtime=runtime())
        assert await derived_of(session, ids.part) == incremental
        async with session.begin():
            report = await verify(session, DOMAIN, "main", runtime=runtime(), derived=True)
        assert report.ok, report.divergences[:3]

    async with keeper(seeded.app_url) as http:
        part = await http.get(f"{BASE}/kinds/Part/records/P-10001")
        assert part.status_code == 200, part.text
        values = part.json()["record"]["values"]
        assert (int(values["supplier_count"]), values["single_sourced"]) == (1, True)
        single = await http.post(
            f"{BASE}/query",
            json={
                "kind": "Part",
                "where": {"attr": "single_sourced", "op": "eq", "value": True},
                "limit": 500,
            },
        )
        assert single.status_code == 200, single.text
        assert len(single.json()["items"]) == sum(1 for c in expected.values() if c == 1)


async def test_schema_changes_recompute_and_verify_finds_divergences(seeded: Seeded) -> None:
    ids = Ids(seeded)
    async with owner_session(seeded.owner_url) as session:
        before = await derived_of(session, ids.part)
        single = ids.schema.derivation_by_api_name("single_sourced")
        assert single is not None
        definition = single.definition.model_copy(
            update={"expression": "size(links.supplied_by) == 1"}
        )
        await commit(
            session,
            DOMAIN,
            "main",
            [],
            schema_changes=[SchemaUpsert(single.model_copy(update={"definition": definition}))],
            author=AUTHOR,
            message="Count the Links directly",
            origin=Origin(type="test"),
            runtime=runtime(),
            clock=Clock(),
        )
        assert await derived_of(session, ids.part) == before

        async with session.begin():
            await scope_to_domain(session, DOMAIN)
            await session.execute(
                text(
                    "UPDATE mat_records SET derived = '{}' WHERE domain_id = :d "
                    "AND branch = 'main' AND key = 'P-10002'"
                ),
                {"d": DOMAIN},
            )
            report = await verify(session, DOMAIN, "main", runtime=runtime(), derived=True)
        [divergence] = report.divergences
        assert divergence.reason is DivergenceReason.DERIVED_MISMATCH
        assert divergence.key == "P-10002"
        assert divergence.actual == {}
