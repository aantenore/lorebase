"""Path Derivations: materialized Links kept current by commits (overlays included) and rewritten
when the fan-out setting changes, composed Links at read time, both readable through the API at
the head and at past Revisions, with the same fan-out bound; followed by neighborhoods, Link
counts, Operation `links()` and Derivations, and only by callers who may read every Kind they
cross."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Mapping
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.model import (
    Derivation,
    Link,
    LinkDirection,
    PathDerivation,
    PathStep,
    Schema,
    new_id,
    schema_from_document,
    schema_to_document,
)
from lorebase_core.model.ids import IdPrefix
from lorebase_core.revision import Origin
from lorebase_core.validate import LinkDelete, LinkUpsert
from lorebase_keeper.auth.api_keys import ApiKeyCodec
from lorebase_keeper.auth.config import ApiKeySettings
from lorebase_store.auth import SqlAuthStore
from lorebase_store.commit import SchemaUpsert, commit
from lorebase_store.db import create_engine
from lorebase_store.materialize import (
    DivergenceReason,
    MaterializationMode,
    create_branch,
    rebuild,
    verify,
)
from lorebase_store.materialize.paths import refresh_path_fanout, refresh_stale_paths
from lorebase_store.models.enums import GrantScope, PrincipalType
from lorebase_store.repositories.scope import scope_to_domain
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, StoreSettings

from .read_fixtures import Seeded, keeper, owner_session, seeded_database
from .supply import AUTHOR, DOMAIN, Clock, commit_changes, load_supply, record_change, rows

pytestmark = pytest.mark.integration

BASE = f"/api/v1/domains/{DOMAIN}"
FANOUT = StoreSettings().traverse.path_fanout
PAIRS = (
    "SELECT from_key, to_key FROM mat_links WHERE domain_id = :d AND branch = :b "
    "AND relation_id = :r AND NOT deleted"
)
OVERLAY = "agent/paths"
PEPPER = "path-auth-pepper-0123456789-0123456789"
CODEC = ApiKeyCodec(ApiKeySettings(pepper=PEPPER))

Pairs = set[tuple[str, str]]


def runtime() -> StoreRuntime:
    return StoreRuntime.from_settings(StoreSettings())


def path(
    name: str, inverse: str, steps: list[tuple[str, LinkDirection]], *, materialized: bool
) -> Derivation:
    return Derivation(
        id=new_id(IdPrefix.DERIVATION),
        api_name=name,
        definition=PathDerivation(
            steps=tuple(PathStep(relation=r, direction=d) for r, d in steps),
            inverse_api_name=inverse,
            materialized=materialized,
        ),
    )


def relation_id(schema: Schema, name: str) -> str:
    found = schema.relation_by_api_name(name)
    assert found is not None
    return found[0].id


def paths_of(schema: Schema) -> list[Derivation]:
    supplied_by = relation_id(schema, "supplied_by")
    with_supplier = relation_id(schema, "with_supplier")
    issued_to = relation_id(schema, "issued_to")
    forward, inverse = LinkDirection.FORWARD, LinkDirection.INVERSE
    contracts = [(supplied_by, forward), (with_supplier, inverse)]
    orders = [(issued_to, forward), (supplied_by, inverse)]
    return [
        path("supplier_contracts", "contracted_parts", contracts, materialized=True),
        path("supplier_contracts_live", "contracted_parts_live", contracts, materialized=False),
        path("order_parts", "ordering_parts", orders, materialized=True),
        path("order_parts_live", "ordering_parts_live", orders, materialized=False),
        path("co_supplied", "co_supplied_by", [(supplied_by, forward), (supplied_by, inverse)],
             materialized=True),
    ]  # fmt: skip


# Expected pairs, computed from the example rows -------------------------------------------------


class Data:
    """The Links the paths read, as plain maps (updated by the tests as they commit)."""

    def __init__(self) -> None:
        self.supplied_by: set[tuple[str, str]] = {
            (r["from_key"], r["to_key"]) for r in rows("supplied_by.csv")
        }
        self.with_supplier = {(r["from_key"], r["to_key"]) for r in rows("with_supplier.csv")}
        self.issued_to = {(r["po_number"], r["supplier"]) for r in rows("purchase_order.csv")}

    def expected(self, name: str, fanout: int = FANOUT) -> Pairs:
        def by(pairs: set[tuple[str, str]], *, reverse: bool) -> dict[str, list[str]]:
            out: dict[str, list[str]] = defaultdict(list)
            for a, b in pairs:
                near, far = (b, a) if reverse else (a, b)
                out[near].append(far)
            return {near: sorted(set(fars))[:fanout] for near, fars in out.items()}

        first, second = {
            "supplier_contracts": (self.supplied_by, (self.with_supplier, True)),
            "order_parts": (self.issued_to, (self.supplied_by, True)),
            "co_supplied": (self.supplied_by, (self.supplied_by, True)),
        }[name.removesuffix("_live")]
        later = by(second[0], reverse=second[1])
        return {(a, c) for a, b in first for c in later.get(b, [])}


async def stored(session: AsyncSession, branch: str, derivation_id: str) -> Pairs:
    async with session.begin():
        await scope_to_domain(session, DOMAIN)
        found = await session.execute(text(PAIRS), {"d": DOMAIN, "b": branch, "r": derivation_id})
        return {(f, t) for f, t in found}


async def seed(
    session: AsyncSession, store: StoreRuntime, clock: Clock, owner_url: str, app_url: str
) -> Seeded:
    schema, _ = await load_supply(session, store, clock)
    added = paths_of(schema)
    result = await commit(
        session,
        DOMAIN,
        "main",
        [],
        schema_changes=[SchemaUpsert(d) for d in added],
        author=AUTHOR,
        message="Add path Derivations",
        origin=Origin(type="test"),
        runtime=store,
        clock=clock,
    )
    schema = schema.model_copy(
        update={"derivations": {**schema.derivations, **{d.id: d for d in added}}}
    )
    engine = create_engine(DatabaseSettings(), url=owner_url, pooled=False)
    try:
        auth = SqlAuthStore(async_sessionmaker(engine, expire_on_commit=False))
        async with auth.unit() as unit:
            # reads Parts and Contracts, not the Suppliers the contract paths cross
            principal = await unit.principals.add(PrincipalType.SERVICE, "parts and contracts")
            for kind in ("Part", "Contract"):
                await unit.grants.add(
                    domain_id=DOMAIN,
                    subject=principal.id,
                    role="viewer",
                    created_by="usr_seed",
                    scope_type=GrantScope.KIND,
                    scope_ref=kind,
                )
            generated = CODEC.generate()
            await unit.api_keys.add(
                principal_id=principal.id,
                prefix=generated.prefix,
                key_hash=generated.key_hash,
                scopes=[f"domain:{DOMAIN}"],
            )
    finally:
        await engine.dispose()
    return Seeded(
        owner_url=owner_url,
        app_url=app_url,
        schema=schema,
        revisions={"paths": result.revision_id.hex()},
        facts={"narrow_key": generated.token},
    )


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


def path_id(seeded: Seeded, name: str) -> str:
    derivation = seeded.schema.derivation_by_api_name(name)
    assert derivation is not None
    return derivation.id


async def links(http: httpx.AsyncClient, path_: str, **params: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = None
    while True:
        query = {**params, "limit": 500, **({"cursor": cursor} if cursor else {})}
        response = await http.get(BASE + path_, params=query)
        assert response.status_code == 200, response.text
        body = response.json()
        items.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return items


async def relation_pairs(http: httpx.AsyncClient, name: str, **params: Any) -> Pairs:
    found = await links(http, f"/relations/{name}/links", **params)
    return {(i["from_key"], i["to_key"]) for i in found}


def grouped(pairs: Pairs) -> Mapping[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for a, c in pairs:
        out[a].add(c)
    return out


# Tests --------------------------------------------------------------------------------------


async def test_reads_compose_or_read_stored_links_alike(seeded: Seeded) -> None:
    data = Data()
    async with owner_session(seeded.owner_url) as session:
        for name in ("supplier_contracts", "order_parts", "co_supplied"):
            assert await stored(session, "main", path_id(seeded, name)) == data.expected(name)
    contracts = data.expected("supplier_contracts")
    part = next(iter(sorted(grouped(contracts))))
    async with keeper(seeded.app_url) as http:
        for name in ("supplier_contracts", "supplier_contracts_live"):
            assert await relation_pairs(http, name) == contracts
            forward = await links(http, f"/kinds/Part/records/{part}/links/{name}")
            assert [i["to_key"] for i in forward] == sorted(grouped(contracts)[part])
            assert {i["target"]["kind"] for i in forward} == {"Contract"}
            assert all(i["values"] == {} for i in forward)
            contract = forward[0]["to_key"]
            inverse = name.replace("supplier_contracts", "contracted_parts")
            back = await links(http, f"/kinds/Contract/records/{contract}/links/{inverse}")
            assert {i["from_key"] for i in back} == {a for a, c in contracts if c == contract}
            assert all(i["direction"] == "inverse" for i in back)
        orders = data.expected("order_parts")
        for name in ("order_parts", "order_parts_live"):  # a foreign-key first step
            assert await relation_pairs(http, name) == orders
            assert await relation_pairs(http, name, from_key="PO-200001") == {
                p for p in orders if p[0] == "PO-200001"
            }
        # a path may lead back to its start: a Part is co-supplied with itself
        co = await relation_pairs(http, "co_supplied")
        assert co == data.expected("co_supplied")
        assert all((p, p) in co for p, _ in data.supplied_by)

        traversed = await http.post(
            f"{BASE}/traverse",
            json={
                "start": {"kind": "Part", "key": part},
                "path": [{"relation": "supplier_contracts_live"}, {"relation": "with_supplier"}],
            },
        )
        assert traversed.status_code == 200, traversed.text
        hops = traversed.json()["hops"]
        assert {r["key"] for r in hops[0]["records"]} == grouped(contracts)[part]

        for name in ("supplier_contracts", "supplier_contracts_live"):
            queried = await http.post(
                f"{BASE}/query",
                json={
                    "kind": "Part",
                    "where": {"count": name, "op": "gte", "value": 2},
                    "include": {name: {"limit": 50}},
                    "limit": 500,
                },
            )
            assert queried.status_code == 200, queried.text
            items = queried.json()["items"]
            assert {i["key"] for i in items} == {
                a for a, cs in grouped(contracts).items() if len(cs) >= 2
            }
            for item in items:
                assert {r["key"] for r in item["included"][name]} == grouped(contracts)[item["key"]]


async def test_composed_reads_follow_the_fanout_bound(seeded: Seeded) -> None:
    data = Data()
    async with keeper(seeded.app_url, store={"traverse": {"path_fanout": 1}}) as http:
        assert await relation_pairs(http, "order_parts_live") == data.expected(
            "order_parts", fanout=1
        )
        assert await relation_pairs(http, "supplier_contracts_live") == data.expected(
            "supplier_contracts", fanout=1
        )


async def test_materialized_paths_follow_commits_and_rebuilds(seeded: Seeded) -> None:
    data = Data()
    schema = seeded.schema
    supplied_by = relation_id(schema, "supplied_by")
    with_supplier = relation_id(schema, "with_supplier")
    order_kind = schema.kind_by_api_name("PurchaseOrder")
    assert order_kind is not None
    clock = Clock()
    async with owner_session(seeded.owner_url) as session:
        before = await commit_changes(
            session,
            runtime(),
            clock,
            [LinkUpsert(link=Link(relation=supplied_by, from_key="P-10001", to_key="S-1001"))],
            message="Add a supplier",
        )
        data.supplied_by.add(("P-10001", "S-1001"))
        moved = next(r for r in rows("purchase_order.csv") if r["po_number"] == "PO-200001")
        dropped = sorted(data.with_supplier)[0]
        moved_to = "S-1002" if dropped[1] != "S-1002" else "S-1003"
        await commit_changes(
            session,
            runtime(),
            clock,
            [
                record_change(order_kind, {**moved, "supplier": "S-1002"}),
                LinkDelete(relation=with_supplier, from_key=dropped[0], to_key=dropped[1]),
                LinkUpsert(link=Link(relation=with_supplier, from_key=dropped[0], to_key=moved_to)),
            ],
            message="Move an order and a contract",
        )
        data.issued_to = (data.issued_to - {("PO-200001", "S-1005")}) | {("PO-200001", "S-1002")}
        data.with_supplier = (data.with_supplier - {dropped}) | {(dropped[0], moved_to)}
        for name in ("supplier_contracts", "order_parts", "co_supplied"):
            assert await stored(session, "main", path_id(seeded, name)) == data.expected(name)

        incremental = {
            name: await stored(session, "main", path_id(seeded, name))
            for name in ("supplier_contracts", "order_parts", "co_supplied")
        }
        async with session.begin():
            await rebuild(session, DOMAIN, "main", runtime=runtime())
        for name, pairs in incremental.items():
            assert await stored(session, "main", path_id(seeded, name)) == pairs
        async with session.begin():
            report = await verify(session, DOMAIN, "main", runtime=runtime(), derived=True)
        assert report.ok, report.divergences[:3]

        async with session.begin():
            await scope_to_domain(session, DOMAIN)
            await session.execute(
                text(
                    "DELETE FROM mat_links WHERE domain_id = :d AND branch = 'main' "
                    "AND relation_id = :r AND from_key = 'P-10001'"
                ),
                {"d": DOMAIN, "r": path_id(seeded, "supplier_contracts")},
            )
            report = await verify(session, DOMAIN, "main", runtime=runtime(), derived=True)
            assert report.divergences
            assert {d.reason for d in report.divergences} == {DivergenceReason.DERIVED_MISMATCH}
            await session.rollback()

    async with keeper(seeded.app_url) as http:
        # the tree at an earlier Revision composes the path from the Links it held then
        original = Data()
        for name in ("supplier_contracts", "supplier_contracts_live"):
            at_paths = await relation_pairs(http, name, at=seeded.revisions["paths"])
            assert at_paths == original.expected("supplier_contracts")
        after_first = await relation_pairs(http, "co_supplied", at=before.revision_id.hex())
        original.supplied_by.add(("P-10001", "S-1001"))
        assert after_first == original.expected("co_supplied")
        tree = await http.get(
            f"{BASE}/kinds/PurchaseOrder/records/PO-200001/links/order_parts",
            params={"at": seeded.revisions["paths"]},
        )
        assert tree.status_code == 200, tree.text
        assert tree.json()["served_from"] == "tree"
        assert {i["to_key"] for i in tree.json()["items"]} == {
            c for a, c in Data().expected("order_parts") if a == "PO-200001"
        }
        back = await links(
            http,
            "/kinds/Part/records/P-10001/links/ordering_parts_live",
            at=seeded.revisions["paths"],
        )
        assert {i["from_key"] for i in back} == {
            a for a, c in Data().expected("order_parts") if c == "P-10001"
        }
        # neighborhoods and Link counts at that Revision read a snapshot of it
        contracts = Data().expected("supplier_contracts")
        part = sorted(grouped(contracts))[0]
        for name in ("supplier_contracts", "supplier_contracts_live"):
            past = await http.post(
                f"{BASE}/neighborhood",
                json={
                    "seeds": [{"kind": "Part", "key": part}],
                    "depth": 1,
                    "relations": [name],
                    "at": seeded.revisions["paths"],
                },
            )
            assert past.status_code == 200, past.text
            assert edges(past.json(), name) == {(part, c) for c in grouped(contracts)[part]}
        record = await http.get(
            f"{BASE}/kinds/Part/records/{part}",
            params={"links": True, "at": seeded.revisions["paths"]},
        )
        assert record.status_code == 200, record.text
        found = {
            (c["relation"], c["direction"]): c["count"] for c in record.json()["record"]["links"]
        }
        for name in ("supplier_contracts", "supplier_contracts_live"):
            assert found[(name, "forward")] == len(grouped(contracts)[part])


async def test_overlay_branches_keep_their_own_path_links(seeded: Seeded) -> None:
    schema = seeded.schema
    supplied_by = relation_id(schema, "supplied_by")
    contracts = path_id(seeded, "supplier_contracts")
    clock = Clock()
    async with owner_session(seeded.owner_url) as session:
        await create_branch(
            session,
            DOMAIN,
            OVERLAY,
            created_by="usr_editor",
            mode=MaterializationMode.OVERLAY,
            runtime=runtime(),
            clock=clock,
        )
        main_before = await stored(session, "main", contracts)
        await commit_changes(
            session,
            runtime(),
            clock,
            [LinkDelete(relation=supplied_by, from_key="P-10001", to_key="S-1005")],
            message="Drop a supplier on the overlay",
            branch=OVERLAY,
        )
        async with session.begin():
            report = await verify(session, DOMAIN, OVERLAY, runtime=runtime(), derived=True)
        assert report.ok, report.divergences[:3]
        assert await stored(session, "main", contracts) == main_before

    async with keeper(seeded.app_url) as http:
        on_overlay = await relation_pairs(http, "supplier_contracts", branch=OVERLAY)
        live = await relation_pairs(http, "supplier_contracts_live", branch=OVERLAY)
        assert await relation_pairs(http, "supplier_contracts") == main_before
        overlay = Data()
        overlay.supplied_by = await relation_pairs(http, "supplied_by", branch=OVERLAY)
        overlay.with_supplier = await relation_pairs(http, "with_supplier", branch=OVERLAY)
        assert ("P-10001", "S-1005") not in overlay.supplied_by
        assert on_overlay == live == overlay.expected("supplier_contracts")
        assert on_overlay != main_before


async def current(http: httpx.AsyncClient) -> Data:
    """The Links the paths read on `main` now (earlier tests change them)."""
    data = Data()
    data.supplied_by = await relation_pairs(http, "supplied_by")
    data.with_supplier = await relation_pairs(http, "with_supplier")
    data.issued_to = await relation_pairs(http, "issued_to")
    return data


async def counts(http: httpx.AsyncClient, kind: str, key: str, **headers: str) -> dict[str, int]:
    response = await http.get(
        f"{BASE}/kinds/{kind}/records/{key}", params={"links": True}, headers=headers
    )
    assert response.status_code == 200, response.text
    return {
        f"{c['relation']}:{c['direction']}": c["count"] for c in response.json()["record"]["links"]
    }


async def hood(
    http: httpx.AsyncClient, kind: str, key: str, relations: list[str], **headers: str
) -> dict[str, Any]:
    response = await http.post(
        f"{BASE}/neighborhood",
        json={"seeds": [{"kind": kind, "key": key}], "depth": 2, "relations": relations},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def edges(body: Mapping[str, Any], relation: str) -> Pairs:
    return {(e["from"]["key"], e["to"]["key"]) for e in body["edges"] if e["relation"] == relation}


async def test_neighborhoods_and_link_counts_follow_paths(seeded: Seeded) -> None:
    async with keeper(seeded.app_url) as http:
        data = await current(http)
        contracts = data.expected("supplier_contracts")
        part = sorted(grouped(contracts))[0]
        reached = grouped(contracts)[part]
        back = {a for a, c in contracts if c in reached}
        for name in ("supplier_contracts", "supplier_contracts_live"):
            # two hops: the Contracts of the Part, then back to every Part they cover
            inverse = name.replace("supplier_contracts", "contracted_parts")
            found = await hood(http, "Part", part, [name, inverse])
            assert {(n["kind"], n["key"]) for n in found["nodes"]} == {
                ("Part", part),
                *(("Contract", c) for c in reached),
                *(("Part", a) for a in back),
            }
            assert edges(found, name) == {(a, c) for a, c in contracts if c in reached}
            assert not found["truncated"]
        # a composed path and a stored Relation in one walk
        mixed = await hood(http, "Part", part, ["supplier_contracts_live", "with_supplier"])
        assert edges(mixed, "supplier_contracts_live") == {(part, c) for c in reached}
        assert edges(mixed, "with_supplier") == {
            (c, s) for c, s in data.with_supplier if c in reached
        }

        found_counts = await counts(http, "Part", part)
        for name in ("supplier_contracts", "supplier_contracts_live"):
            assert found_counts[f"{name}:forward"] == len(reached)
        assert found_counts["co_supplied:forward"] == len(
            {c for a, c in data.expected("co_supplied") if a == part}
        )
        contract = sorted(reached)[0]
        contract_counts = await counts(http, "Contract", contract)
        for name in ("contracted_parts", "contracted_parts_live"):
            assert contract_counts[f"{name}:inverse"] == len(
                {a for a, c in contracts if c == contract}
            )


async def test_paths_need_read_access_to_every_kind_crossed(seeded: Seeded) -> None:
    """A caller reading Parts and Contracts, not Suppliers: following a contract path is
    refused, and neighborhoods and Link counts leave it out instead of leaking it."""
    auth = {"Authorization": f"Bearer {seeded.facts['narrow_key']}"}
    section = {"auth": {"mode": "api_key", "api_keys": {"pepper": PEPPER}}}
    async with keeper(seeded.app_url) as http:
        contracts = (await current(http)).expected("supplier_contracts")
    part = sorted(grouped(contracts))[0]
    async with keeper(seeded.app_url, section) as http:
        for name in ("supplier_contracts", "supplier_contracts_live"):
            refused = await http.get(f"{BASE}/kinds/Part/records/{part}/links/{name}", headers=auth)
            assert refused.status_code == 403, refused.text
            listed = await http.get(f"{BASE}/relations/{name}/links", headers=auth)
            assert listed.status_code == 403, listed.text
        assert (
            await http.get(f"{BASE}/kinds/Part/records/{part}", headers=auth)
        ).status_code == 200

        found_counts = await counts(http, "Part", part, **auth)
        assert not {k for k in found_counts if k.startswith("supplier_contracts")}
        assert "co_supplied:forward" not in found_counts  # crosses Supplier too

        found = await hood(
            http, "Part", part, ["supplier_contracts", "supplier_contracts_live"], **auth
        )
        assert {n["kind"] for n in found["nodes"]} == {"Part"}
        assert found["edges"] == []


async def test_derivations_and_operations_read_paths(seeded: Seeded) -> None:
    patch: dict[str, dict[str, Any]] = {
        "kinds": {
            "Part": {
                "attributes": {
                    "contract_count": {
                        "type": "integer",
                        "description": "Contracts of the Part's suppliers.",
                        "derivation": "part_contract_count",
                    }
                }
            },
            "Contract": {
                "attributes": {
                    "part_count": {
                        "type": "integer",
                        "description": "Parts its supplier supplies.",
                        "derivation": "contract_part_count",
                    }
                }
            },
        },
        "derivations": {
            "part_contract_count": {
                "definition": {
                    "variant": "aggregate",
                    "kind": "Part",
                    "attribute": "contract_count",
                    "relation": "supplier_contracts_live",
                    "function": "count",
                }
            },
            "contract_part_count": {
                "definition": {
                    "variant": "expression",
                    "kind": "Contract",
                    "attribute": "part_count",
                    "expression": "size(links.contracted_parts)",
                }
            },
        },
        "operations": {
            "check_contracts": {
                "description": "Checks the contract paths of a Part and a Contract.",
                "parameters": [
                    {"name": "part", "type": "ref(Part)", "required": True},
                    {"name": "contract", "type": "ref(Contract)", "required": True},
                    {"name": "contracts", "type": "integer", "required": True},
                    {"name": "parts", "type": "integer", "required": True},
                ],
                "preconditions": [
                    {
                        "expression": 'size(links(part, "supplier_contracts_live")) == contracts',
                        "message": "Contracts differ.",
                    },
                    {
                        "expression": 'size(links(contract, "contracted_parts")) == parts',
                        "message": "Parts differ.",
                    },
                ],
            }
        },
    }

    async def derived(http: httpx.AsyncClient, kind: str, key: str, name: str) -> Any:
        response = await http.get(f"{BASE}/kinds/{kind}/records/{key}")
        assert response.status_code == 200, response.text
        return response.json()["record"]["values"].get(name)

    async def check(http: httpx.AsyncClient) -> None:
        contracts = (await current(http)).expected("supplier_contracts")
        by_part = grouped(contracts)
        for part in sorted(by_part)[:5]:
            assert await derived(http, "Part", part, "contract_count") == str(len(by_part[part]))
        for contract in sorted({c for _, c in contracts})[:5]:
            parts = len({a for a, c in contracts if c == contract})
            assert await derived(http, "Contract", contract, "part_count") == str(parts)

    document = schema_to_document(seeded.schema)
    for section, entries in patch.items():
        for name, entry in entries.items():
            merged = document[section].setdefault(name, {})
            for field, value in entry.items():
                merged[field] = (
                    {**merged.get(field, {}), **value} if isinstance(value, dict) else value
                )
    changed = schema_from_document(document, base=seeded.schema)
    async with owner_session(seeded.owner_url) as session:
        await commit(
            session,
            DOMAIN,
            "main",
            [],
            schema_changes=[
                SchemaUpsert(element)
                for section in ("kinds", "derivations", "operations")
                for element in getattr(changed, section).values()
                if element.api_name in patch[section]
            ],
            author=AUTHOR,
            message="Derive values over paths",
            origin=Origin(type="test"),
            runtime=runtime(),
            clock=Clock(),
        )
    async with keeper(seeded.app_url) as http:
        await check(http)

        contracts = (await current(http)).expected("supplier_contracts")
        part = sorted(grouped(contracts))[0]
        contract = sorted(grouped(contracts)[part])[0]
        parts = len({a for a, c in contracts if c == contract})
        parameters = {"part": part, "contract": contract}
        body = {"branch": "main", "dry_run": True}
        ok = await http.post(
            f"{BASE}/operations/check_contracts/execute",
            json={
                **body,
                "parameters": {
                    **parameters,
                    "contracts": len(grouped(contracts)[part]),
                    "parts": parts,
                },
            },
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["violations"] == []
        off = await http.post(
            f"{BASE}/operations/check_contracts/execute",
            json={**body, "parameters": {**parameters, "contracts": 0, "parts": parts + 1}},
        )
        assert off.status_code == 200, off.text
        assert len(off.json()["violations"]) == 2

    # a change two steps away from the Records holding the values updates them incrementally
    schema_now = seeded.schema
    with_supplier = relation_id(schema_now, "with_supplier")
    async with keeper(seeded.app_url) as http:
        data = await current(http)
    moved = sorted(data.with_supplier)[-1]
    target = next(s for _, s in sorted(data.with_supplier) if s != moved[1])
    async with owner_session(seeded.owner_url) as session:
        await commit_changes(
            session,
            runtime(),
            Clock(),
            [
                LinkDelete(relation=with_supplier, from_key=moved[0], to_key=moved[1]),
                LinkUpsert(link=Link(relation=with_supplier, from_key=moved[0], to_key=target)),
            ],
            message="Move a contract to another supplier",
        )
        async with session.begin():
            report = await verify(session, DOMAIN, "main", runtime=runtime(), derived=True)
        assert report.ok, report.divergences[:3]
    async with keeper(seeded.app_url) as http:
        await check(http)


async def test_a_fanout_change_rewrites_materialized_paths(seeded: Seeded) -> None:
    narrow = StoreRuntime.from_settings(StoreSettings(traverse={"path_fanout": 1}))
    async with keeper(seeded.app_url) as http:
        data = await current(http)
    names = ("supplier_contracts", "order_parts", "co_supplied")
    async with owner_session(seeded.owner_url) as session:
        for name in names:
            assert await stored(session, "main", path_id(seeded, name)) == data.expected(name)
        rewritten = await refresh_stale_paths(
            async_sessionmaker(session.bind, expire_on_commit=False), runtime=narrow
        )
        assert "main" in {b for d, b in rewritten if d == DOMAIN}
        for name in names:
            assert await stored(session, "main", path_id(seeded, name)) == data.expected(
                name, fanout=1
            )
        async with session.begin():
            report = await verify(session, DOMAIN, "main", runtime=narrow, derived=True)
        assert report.ok, report.divergences[:3]
        # nothing left to rewrite at the same setting; a commit at the default rewrites back
        async with session.begin():
            assert not await refresh_path_fanout(session, DOMAIN, "main", runtime=narrow)
        supplied_by = relation_id(seeded.schema, "supplied_by")
        part = sorted(a for a, _ in data.supplied_by)[0]
        linked = {s for a, s in data.supplied_by if a == part}
        supplier = sorted({s for _, s in data.supplied_by} - linked)[0]
        await commit_changes(
            session,
            runtime(),
            Clock(),
            [LinkUpsert(link=Link(relation=supplied_by, from_key=part, to_key=supplier))],
            message="Add a supplier Link",
        )
        data.supplied_by.add((part, supplier))
        for name in names:
            assert await stored(session, "main", path_id(seeded, name)) == data.expected(name)
        async with session.begin():
            report = await verify(session, DOMAIN, "main", runtime=runtime(), derived=True)
        assert report.ok, report.divergences[:3]
