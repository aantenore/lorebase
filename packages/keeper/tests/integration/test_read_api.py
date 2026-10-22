"""Read, query, search, revisions and diff endpoints against the `supply` example in Postgres."""

from __future__ import annotations

import json
import random
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Schema
from lorebase_core.query import Query as DslQuery
from lorebase_core.query import QueryLimits, resolve_query
from lorebase_keeper.reading.compiler import Compiler
from lorebase_keeper.reading.settings import SearchSettings
from lorebase_store.embeddings import template_fingerprints
from lorebase_store.indexes import plan_indexes
from lorebase_store.materialize import create_branch, verify
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import StoreSettings

from .read_fixtures import Seeded, keeper, owner_session, seeded_database
from .supply import (
    DOMAIN,
    Clock,
    commit_changes,
    load_supply,
    record_change,
    rows,
    supply_schema,
)

pytestmark = pytest.mark.integration

BASE = f"/api/v1/domains/{DOMAIN}"
DIMS = StoreSettings().embeddings.dims
MODEL = SearchSettings().semantic.model


def unit_vector(index: int) -> list[float]:
    vector = [0.0] * DIMS
    vector[index % DIMS] = 1.0
    vector[(index + 1) % DIMS] = 0.5
    return vector


def with_part_template() -> Schema:
    """The example schema with Parts embedded by name, so their vectors are searched."""
    schema = supply_schema()
    part = schema.kind_by_api_name("Part")
    assert part is not None
    search = part.search.model_copy(update={"embedding_template": "{name}"})
    return schema.model_copy(
        update={"kinds": {**schema.kinds, part.id: part.model_copy(update={"search": search})}}
    )


async def seed(
    session: AsyncSession, runtime: StoreRuntime, clock: Clock, owner_url: str, app_url: str
) -> Seeded:
    schema, (created, schema_commit, data) = await load_supply(
        session, runtime, clock, with_part_template()
    )
    supplier = schema.kind_by_api_name("Supplier")
    assert supplier is not None
    original = next(r for r in rows("supplier.csv") if r["supplier_code"] == "S-1001")
    updated = await commit_changes(
        session,
        runtime,
        clock,
        [record_change(supplier, {**original, "risk_score": "90"})],
        message="Raise the risk of S-1001",
    )
    await create_branch(
        session, DOMAIN, "feature/risk", created_by="usr_editor", runtime=runtime, clock=clock
    )
    feature = await commit_changes(
        session,
        runtime,
        clock,
        [
            record_change(
                supplier,
                {
                    "supplier_code": "S-9999",
                    "legal_name": "Branchonly Bearings GmbH",
                    "country": "DE",
                    "tier": "2",
                    "status": "active",
                },
            )
        ],
        message="Add a supplier on a branch",
        branch="feature/risk",
    )
    async with session.begin():
        found = await session.execute(
            text(
                "SELECT key, object_hash FROM mat_records WHERE domain_id = :d AND branch = 'main' "
                "AND kind_id = :k AND key IN ('P-10001', 'P-10002', 'P-10003', 'P-10004')"
            ),
            {"d": DOMAIN, "k": schema.kind_by_api_name("Part").id},  # type: ignore[union-attr]
        )
        part = schema.kind_by_api_name("Part")
        assert part is not None
        fingerprint = template_fingerprints([part])[part.id]
        for index, (key, digest) in enumerate(sorted(found)):
            # P-10004's only vector was rendered by another template: never served here.
            stale = key == "P-10004"
            await session.execute(
                text(
                    "INSERT INTO embeddings "
                    "(domain_id, object_hash, model, fingerprint, dims, embedding) "
                    "VALUES (:d, :h, :m, :f, :n, CAST(:v AS vector))"
                ),
                {
                    "d": DOMAIN,
                    "h": digest,
                    "m": MODEL,
                    "f": "another-template" if stale else fingerprint,
                    "n": DIMS,
                    "v": str(unit_vector(0 if stale else index)),
                },
            )
        for branch in ("main", "feature/risk"):
            report = await verify(session, DOMAIN, branch, runtime=runtime)
            assert report.ok, report
    return Seeded(
        owner_url=owner_url,
        app_url=app_url,
        schema=schema,
        revisions={
            "created": created.revision_id.hex(),
            "schema": schema_commit.revision_id.hex(),
            "data": data.revision_id.hex(),
            "updated": updated.revision_id.hex(),
            "feature": feature.revision_id.hex(),
        },
    )


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


@pytest.fixture
async def http(seeded: Seeded) -> Any:
    async with keeper(seeded.app_url) as client:
        yield client


async def get_ok(http: httpx.AsyncClient, path: str, **params: Any) -> Any:
    response = await http.get(BASE + path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def post_ok(http: httpx.AsyncClient, path: str, body: dict[str, Any]) -> Any:
    response = await http.post(BASE + path, json=body)
    assert response.status_code == 200, response.text
    return response.json()


# Records ----------------------------------------------------------------------------------


async def test_get_record_with_links_lineage_and_etag(
    http: httpx.AsyncClient, seeded: Seeded
) -> None:
    response = await http.get(f"{BASE}/kinds/Supplier/records/S-1001", params={"links": True})
    assert response.status_code == 200
    body = response.json()
    assert response.headers["Lorebase-Revision"] == seeded.revisions["updated"]
    assert body["revision"] == seeded.revisions["updated"]
    assert body["served_from"] == "materialized"
    record = body["record"]
    assert record["kind"] == "Supplier"
    assert record["title"] == "Kavsen Alloys Sp. z o.o."
    assert record["values"]["risk_score"] == "90"
    assert record["updated_revision"] == seeded.revisions["updated"]
    links = {link["relation"]: link["count"] for link in record["links"]}
    with_supplier = [r for r in rows("with_supplier.csv") if r["to_key"] == "S-1001"]
    assert links["contracts"] == len(with_supplier)
    assert links["supplies"] == sum(1 for r in rows("supplied_by.csv") if r["to_key"] == "S-1001")
    purchase_orders = [r for r in rows("purchase_order.csv") if r["supplier"] == "S-1001"]
    assert links["purchase_orders"] == len(purchase_orders)

    etag = response.headers["ETag"]
    assert etag.startswith(f'"{seeded.revisions["updated"]}.')
    assert response.headers["Cache-Control"] == "private, must-revalidate"
    path = f"{BASE}/kinds/Supplier/records/S-1001"
    again = await http.get(path, params={"links": True}, headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.headers["ETag"] == etag
    assert again.headers["Lorebase-Revision"] == seeded.revisions["updated"]
    at_head = await http.get(
        path,
        params={"links": True, "branch": "main"},
        headers={"If-None-Match": etag},
    )
    assert at_head.status_code == 200, "another query string, another tag"
    cached = await http.get(path, params={"links": True})
    assert cached.status_code == 200
    assert cached.json() == body
    other = await http.get(path, headers={"If-None-Match": etag})
    assert other.status_code == 200  # other parameters, other tag

    lineage = (await get_ok(http, "/kinds/Supplier/records/S-1001", lineage=True))["record"]
    assert lineage["lineage"]["risk_score"]["origin_type"] == "operation"
    assert lineage["lineage"]["legal_name"]["origin_type"] == "import"

    missing = await http.get(f"{BASE}/kinds/Supplier/records/S-0000")
    assert missing.status_code == 404
    assert missing.headers["content-type"] == "application/problem+json"
    assert (await http.get(f"{BASE}/kinds/Nope/records/S-1001")).status_code == 404
    assert (await http.get("/api/v1/domains/nope/kinds/Supplier/records/x")).status_code == 404


async def test_time_travel_reads_the_tree(http: httpx.AsyncClient, seeded: Seeded) -> None:
    before = await get_ok(http, "/kinds/Supplier/records/S-1001", at=seeded.revisions["data"])
    assert before["served_from"] == "tree"
    assert before["revision"] == seeded.revisions["data"]
    assert before["record"]["values"]["risk_score"] == "32"
    assert before["record"]["title"] == "Kavsen Alloys Sp. z o.o."

    # at the head the materialized rows serve the read
    head = await get_ok(http, "/kinds/Supplier/records/S-1001", at=seeded.revisions["updated"])
    assert head["served_from"] == "materialized"

    keys: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"at": seeded.revisions["data"], "limit": 7}
        if cursor:
            params["cursor"] = cursor
        page = await get_ok(http, "/kinds/Supplier/records", **params)
        assert page["served_from"] == "tree"
        keys.extend(item["key"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert keys == sorted(r["supplier_code"] for r in rows("supplier.csv"))

    links = await get_ok(
        http, "/kinds/Part/records/P-10001/links/supplied_by", at=seeded.revisions["data"]
    )
    expected = sorted(r["to_key"] for r in rows("supplied_by.csv") if r["from_key"] == "P-10001")
    assert [link["to_key"] for link in links["items"]] == expected

    before_schema = await http.get(
        f"{BASE}/kinds/Supplier/records/S-1001", params={"at": seeded.revisions["created"]}
    )
    assert before_schema.status_code == 404  # no Supplier Kind yet

    refused = await http.get(
        f"{BASE}/kinds/Supplier/records",
        params={
            "at": seeded.revisions["data"],
            "where": '{"attr":"country","op":"eq","value":"PL"}',
        },
    )
    assert refused.status_code == 422
    assert refused.json()["errors"][0]["code"] == "time_travel"
    unknown = await http.get(f"{BASE}/kinds/Supplier/records/S-1001", params={"at": "f" * 64})
    assert unknown.status_code == 404


async def _trait_pages(
    http: httpx.AsyncClient, body: dict[str, Any], limit: int
) -> tuple[list[tuple[str, str]], list[str | None]]:
    """Every Record of a Trait query, page by page, and the cursors issued."""
    found: list[tuple[str, str]] = []
    cursors: list[str | None] = []
    cursor = None
    while True:
        page = await post_ok(http, "/query", {**body, "limit": limit, "cursor": cursor})
        found.extend((item["kind"], item["key"]) for item in page["items"])
        cursor = page["next_cursor"]
        cursors.append(cursor)
        if cursor is None:
            return found, cursors


async def test_trait_queries_at_a_past_revision_match_the_head(
    http: httpx.AsyncClient, seeded: Seeded
) -> None:
    at = seeded.revisions["data"]
    everyone = sorted(
        [("Supplier", r["supplier_code"]) for r in rows("supplier.csv")]
        + [("Plant", r["plant_code"]) for r in rows("plant.csv")]
    )
    for order in ([], [{"attr": "key"}]):
        body: dict[str, Any] = {"trait": "Located", "order_by": order, "select": ["country"]}
        head, head_cursors = await _trait_pages(http, body, 7)
        past, past_cursors = await _trait_pages(http, {**body, "at": at}, 7)
        assert past == head
        assert sorted(past) == everyone
        assert past_cursors == head_cursors  # the same positions at the head and in the tree
        if order:
            assert [key for _, key in past] == sorted(key for _, key in past)

    page = await post_ok(http, "/query", {"trait": "Located", "at": at, "limit": 3})
    assert page["served_from"] == "tree"
    assert all(
        set(item["values"]) <= {"country", "city"} or item["values"] for item in page["items"]
    )
    refused = await http.post(
        f"{BASE}/query",
        json={
            "trait": "Located",
            "at": at,
            "where": {"attr": "country", "op": "eq", "value": "IT"},
        },
    )
    assert refused.status_code == 422
    assert refused.json()["errors"][0]["code"] == "time_travel"


async def test_list_with_where_order_select_and_cursor(http: httpx.AsyncClient) -> None:
    where = json.dumps({"attr": "country", "op": "in", "value": ["PL", "DE", "IT"]})
    seen: list[dict[str, Any]] = []
    cursor = None
    while True:
        params: dict[str, Any] = {
            "where": where,
            "order_by": "risk_score:desc,key",
            "select": "risk_score,country",
            "limit": 4,
        }
        if cursor:
            params["cursor"] = cursor
        page = await get_ok(http, "/kinds/Supplier/records", **params)
        seen.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    data = [
        r | ({"risk_score": "90"} if r["supplier_code"] == "S-1001" else {})
        for r in rows("supplier.csv")
    ]
    expected = sorted(
        (r for r in data if r["country"] in ("PL", "DE", "IT")),
        key=lambda r: (-int(r["risk_score"]), r["supplier_code"]),
    )
    assert [item["key"] for item in seen] == [r["supplier_code"] for r in expected]
    assert all(set(item["values"]) <= {"risk_score", "country"} for item in seen)

    bad = await http.get(
        f"{BASE}/kinds/Supplier/records",
        params={"where": json.dumps({"attr": "risk_score", "op": "prefix", "value": "1"})},
    )
    assert bad.status_code == 422
    assert bad.json()["errors"][0]["pointer"] == "/query/where/op"
    foreign = await http.get(f"{BASE}/kinds/Part/records", params={"cursor": cursor or "eyJ9"})
    assert foreign.status_code == 422


# Query DSL --------------------------------------------------------------------------------


async def test_query_relations_counts_includes_and_aggregate(http: httpx.AsyncClient) -> None:
    suppliers = {r["supplier_code"]: r for r in rows("supplier.csv")}
    supplied = rows("supplied_by.csv")
    body = {
        "kind": "Part",
        "where": {
            "and": [
                {"via": "supplied_by", "where": {"attr": "country", "op": "in", "value": ["PL"]}},
                {"count": "supplied_by", "op": "gte", "value": 1},
            ]
        },
        "select": ["name"],
        "include": {"supplied_by": {"select": ["country"], "limit": 2}},
        "order_by": [{"attr": "key"}],
        "limit": 100,
        "aggregate": {"group_by": "category"},
    }
    result = await post_ok(http, "/query", body)
    expected = sorted(
        {s["from_key"] for s in supplied if suppliers[s["to_key"]]["country"] == "PL"}
    )
    assert [item["key"] for item in result["items"]] == expected
    first = result["items"][0]
    assert set(first["values"]) <= {"name"}
    included = first["included"]["supplied_by"]
    assert 1 <= len(included) <= 2
    assert all(set(i["values"]) <= {"country"} for i in included)
    parts = {r["part_number"]: r for r in rows("part.csv")}
    counts: dict[str, int] = {}
    for key in expected:
        counts[parts[key]["category"]] = counts.get(parts[key]["category"], 0) + 1
    assert {c["value"]: c["count"] for c in result["aggregate"]["counts"]} == counts

    multi = await post_ok(
        http,
        "/query",
        {"kind": "Part", "where": {"count": "supplied_by", "op": "gte", "value": 2}, "limit": 100},
    )
    per_part: dict[str, int] = {}
    for s in supplied:
        per_part[s["from_key"]] = per_part.get(s["from_key"], 0) + 1
    assert {i["key"] for i in multi["items"]} == {k for k, n in per_part.items() if n >= 2}

    none = await post_ok(
        http,
        "/query",
        {"kind": "Supplier", "where": {"not": {"via": "supplies"}}, "limit": 100},
    )
    with_parts = {s["to_key"] for s in supplied}
    assert {i["key"] for i in none["items"]} == set(suppliers) - with_parts

    via_fk = await post_ok(
        http,
        "/query",
        {
            "kind": "Supplier",
            "where": {
                "via": "purchase_orders",
                "where": {"attr": "status", "op": "eq", "value": "shipped"},
            },
            "limit": 100,
        },
    )
    shipped = {r["supplier"] for r in rows("purchase_order.csv") if r["status"] == "shipped"}
    assert {i["key"] for i in via_fk["items"]} == shipped

    located = await post_ok(
        http,
        "/query",
        {"trait": "Located", "where": {"attr": "country", "op": "eq", "value": "IT"}, "limit": 100},
    )
    italian = {r["supplier_code"] for r in rows("supplier.csv") if r["country"] == "IT"} | {
        r["plant_code"] for r in rows("plant.csv") if r["country"] == "IT"
    }
    assert {i["key"] for i in located["items"]} == italian
    assert {i["kind"] for i in located["items"]} <= {"Supplier", "Plant"}

    text_match = await post_ok(
        http, "/query", {"kind": "Part", "where": {"match": "bearing"}, "limit": 100}
    )
    assert text_match["items"]
    assert all(
        "bearing" in i["title"].lower() or "bearing" in json.dumps(i["values"]).lower()
        for i in text_match["items"]
    )

    invalid = await http.post(
        f"{BASE}/query",
        json={
            "kind": "Part",
            "where": {
                "and": [
                    {"attr": "nope", "op": "eq", "value": 1},
                    {"attr": "unit_cost", "op": "gt", "value": "cheap"},
                    {"via": "flags"},
                ]
            },
        },
    )
    assert invalid.status_code == 422
    pointers = {e["pointer"]: e["code"] for e in invalid.json()["errors"]}
    assert pointers == {
        "/body/where/and/0/attr": "unknown_field",
        "/body/where/and/1/value": "invalid_value",
        "/body/where/and/2/via": "relation_not_on_kind",
    }
    deep = {
        "via": "supplied_by",
        "where": {"via": "supplies", "where": {"via": "supplied_by", "where": {"via": "supplies"}}},
    }
    too_deep = await http.post(f"{BASE}/query", json={"kind": "Part", "where": deep})
    assert too_deep.status_code == 422
    assert too_deep.json()["errors"][0]["code"] == "depth_exceeded"


def _brute(op: str, stored: Any, operand: Any) -> bool:
    if op == "exists":
        return (stored is not None) == operand
    if stored is None:
        return False
    if op == "eq":
        return bool(stored == operand)
    if op == "ne":
        return bool(stored != operand)
    if op == "in":
        return stored in operand
    if op == "prefix":
        return str(stored).startswith(operand)
    if op == "between":
        return bool(operand[0] <= stored <= operand[1])
    return bool(
        {
            "lt": stored < operand,
            "lte": stored <= operand,
            "gt": stored > operand,
            "gte": stored >= operand,
        }[op]
    )


async def test_results_match_a_brute_force_evaluator(http: httpx.AsyncClient) -> None:
    parts = rows("part.csv")
    fields: dict[str, Callable[[dict[str, str]], Any]] = {
        "unit_cost": lambda r: Decimal(r["unit_cost"]) if r["unit_cost"] else None,
        "category": lambda r: r["category"] or None,
        "name": lambda r: r["name"],
        "lifecycle": lambda r: r["lifecycle"] or None,
        "hazardous": lambda r: r["hazardous"] == "true" if r["hazardous"] else None,
    }
    generator = random.Random(20261020)

    def predicate() -> tuple[dict[str, Any], Callable[[dict[str, str]], bool]]:
        name = generator.choice(list(fields))
        read = fields[name]
        values = [read(r) for r in parts if read(r) is not None]
        sample = generator.choice(values)
        if name == "unit_cost":
            op = generator.choice(["eq", "ne", "lt", "lte", "gt", "gte", "between", "exists"])
            if op == "between":
                low, high = sorted([sample, generator.choice(values)])
                return (
                    {"attr": name, "op": op, "value": [str(low), str(high)]},
                    lambda r: _brute(op, read(r), (low, high)),
                )
            operand: Any = True if op == "exists" else sample
            return (
                {"attr": name, "op": op, "value": operand if op == "exists" else str(sample)},
                lambda r: _brute(op, read(r), operand),
            )
        if name == "hazardous":
            op = generator.choice(["eq", "ne"])
            return {"attr": name, "op": op, "value": sample}, lambda r: _brute(op, read(r), sample)
        if name == "name":
            prefix = str(sample)[: generator.randint(1, 4)]
            return (
                {"attr": name, "op": "prefix", "value": prefix},
                lambda r: _brute("prefix", read(r), prefix),
            )
        op = generator.choice(["eq", "ne", "in"])
        operand = [sample, generator.choice(values)] if op == "in" else sample
        return {"attr": name, "op": op, "value": operand}, lambda r: _brute(op, read(r), operand)

    for _ in range(40):
        (left, left_check), (right, right_check) = predicate(), predicate()
        combine = generator.choice(["and", "or", "not"])
        if combine == "not":
            where: dict[str, Any] = {"not": left}

            def check(r: dict[str, str], lc: Any = left_check) -> bool:
                return not lc(r)
        else:
            where = {combine: [left, right]}

            def check(
                r: dict[str, str], lc: Any = left_check, rc: Any = right_check, c: str = combine
            ) -> bool:
                return (lc(r) and rc(r)) if c == "and" else (lc(r) or rc(r))

        result = await post_ok(http, "/query", {"kind": "Part", "where": where, "limit": 500})
        got = sorted(i["key"] for i in result["items"])
        assert got == sorted(r["part_number"] for r in parts if check(r)), where


# Links and graph --------------------------------------------------------------------------


async def test_links_by_record_and_by_relation(http: httpx.AsyncClient) -> None:
    supplied = rows("supplied_by.csv")
    forward = await get_ok(http, "/kinds/Part/records/P-10001/links/supplied_by")
    assert [link["to_key"] for link in forward["items"]] == sorted(
        r["to_key"] for r in supplied if r["from_key"] == "P-10001"
    )
    first = forward["items"][0]
    assert first["target"]["kind"] == "Supplier"
    assert first["target"]["title"]
    assert "is_primary" in first["values"]

    inverse_keys: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        page = await get_ok(http, "/kinds/Supplier/records/S-1005/links/supplies", **params)
        inverse_keys.extend(link["from_key"] for link in page["items"])
        assert all(link["direction"] == "inverse" for link in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert inverse_keys == sorted(r["from_key"] for r in supplied if r["to_key"] == "S-1005")

    by_relation = await get_ok(http, "/relations/supplied_by/links", limit=500)
    assert len(by_relation["items"]) == len(supplied)
    filtered = await get_ok(http, "/relations/supplied_by/links", to_key="S-1005", limit=500)
    assert {link["from_key"] for link in filtered["items"]} == set(inverse_keys)

    issued = await get_ok(http, "/kinds/PurchaseOrder/records/PO-200001/links/issued_to")
    assert [link["to_key"] for link in issued["items"]] == ["S-1005"]
    orders = await get_ok(http, "/relations/issued_to/links", to_key="S-1005", limit=500)
    assert {link["from_key"] for link in orders["items"]} == {
        r["po_number"] for r in rows("purchase_order.csv") if r["supplier"] == "S-1005"
    }
    assert (await http.get(f"{BASE}/kinds/Part/records/P-10001/links/flags")).status_code == 404


async def test_traverse_and_neighborhood(http: httpx.AsyncClient) -> None:
    result = await post_ok(
        http,
        "/traverse",
        {
            "start": {"kind": "Part", "key": "P-10001"},
            "path": [
                {"relation": "supplied_by"},
                {
                    "relation": "contracts",
                    "where": {"attr": "status", "op": "eq", "value": "active"},
                },
            ],
        },
    )
    suppliers = sorted(r["to_key"] for r in rows("supplied_by.csv") if r["from_key"] == "P-10001")
    assert [r["key"] for r in result["hops"][0]["records"]] == suppliers
    contracts = {r["contract_number"]: r for r in rows("contract.csv")}
    expected = sorted(
        r["from_key"]
        for r in rows("with_supplier.csv")
        if r["to_key"] in suppliers and contracts[r["from_key"]]["status"] == "active"
    )
    assert sorted(r["key"] for r in result["hops"][1]["records"]) == expected
    assert result["hops"][0]["edges"][0]["from"]["key"] == "P-10001"

    bad = await http.post(
        f"{BASE}/traverse",
        json={"start": {"kind": "Part", "key": "P-10001"}, "path": [{"relation": "contracts"}]},
    )
    assert bad.status_code == 422

    hood = await post_ok(
        http, "/neighborhood", {"seeds": [{"kind": "Supplier", "key": "S-1005"}], "depth": 1}
    )
    keys = {(n["kind"], n["key"]) for n in hood["nodes"]}
    assert ("Supplier", "S-1005") in keys
    assert {
        ("Part", r["from_key"]) for r in rows("supplied_by.csv") if r["to_key"] == "S-1005"
    } <= keys
    assert not hood["truncated"]
    capped = await post_ok(
        http,
        "/neighborhood",
        {"seeds": [{"kind": "Supplier", "key": "S-1005"}], "depth": 2, "max_nodes": 3},
    )
    assert len(capped["nodes"]) == 3
    assert capped["truncated"]


async def test_indexed_predicates_read_through_the_partial_index(seeded: Seeded) -> None:
    """Compiled comparisons on an indexed Attribute repeat its partial index, so a role not
    subject to RLS reads them through it (sequential scans off: usable, not just chosen)."""
    plan = resolve_query(
        DslQuery.model_validate(
            {"kind": "Supplier", "where": {"attr": "risk_score", "op": "gte", "value": 50}}
        ),
        seeded.schema,
        QueryLimits(),
    )
    assert plan.where is not None
    compiler = Compiler(
        DOMAIN,
        "main",
        text_search_config="simple",
        indexes=plan_indexes(seeded.schema).by_attribute(),
    )
    r = compiler.records("r")
    statement = select(r.c.key).where(
        compiler.record_scope(r, plan.target), compiler.condition(r, plan.where, plan.target)
    )
    sql = str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    async with owner_session(seeded.owner_url) as session, session.begin():
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        connection = await session.connection()
        raw = (await connection.exec_driver_sql(f"EXPLAIN (FORMAT JSON) {sql}")).scalar_one()
    plan_text = json.dumps(raw)
    assert "mat_records_attr_" in plan_text, plan_text


# Search -----------------------------------------------------------------------------------


async def test_lexical_semantic_and_hybrid_search(http: httpx.AsyncClient) -> None:
    lexical = await post_ok(http, "/search", {"q": "bearing", "kinds": ["Part"], "limit": 5})
    assert lexical["mode"] == "lexical"
    assert lexical["items"]
    assert all(hit["kind"] == "Part" for hit in lexical["items"])
    assert "bearing" in (lexical["items"][0]["snippet"] or "").lower()
    scores = [hit["score"] for hit in lexical["items"]]
    assert scores == sorted(scores, reverse=True)

    assert lexical["corrections"] == []
    typo = await post_ok(http, "/search", {"q": "Kavsen Aloys", "kinds": ["Supplier"]})
    assert "S-1001" in [hit["key"] for hit in typo["items"]]
    assert [c["word"] for c in typo["corrections"]] == ["aloys"]
    assert "alloys" in typo["corrections"][0]["replacements"]
    assert "alloys" in (typo["items"][0]["snippet"] or "").lower()
    filtered_typo = await post_ok(
        http,
        "/search",
        {
            "q": "Kavsen Aloys",
            "kinds": ["Supplier"],
            "where": {"attr": "country", "op": "eq", "value": "PL"},
        },
    )
    assert filtered_typo["items"][0]["key"] == "S-1001"
    assert filtered_typo["corrections"] == typo["corrections"]

    filtered = await post_ok(
        http,
        "/search",
        {
            "q": "bearing",
            "kinds": ["Part"],
            "where": {"attr": "lifecycle", "op": "eq", "value": "obsolete"},
        },
    )
    obsolete = {r["part_number"] for r in rows("part.csv") if r["lifecycle"] == "obsolete"}
    assert {hit["key"] for hit in filtered["items"]} <= obsolete

    semantic = await post_ok(http, "/search", {"query_vector": unit_vector(0), "limit": 3})
    assert semantic["mode"] == "semantic"
    assert semantic["items"][0]["key"] == "P-10001"
    assert semantic["items"][0]["score"] == pytest.approx(1.0)
    # Only vectors under the fingerprint of the branch's Part template are searched.
    assert "P-10004" not in {hit["key"] for hit in semantic["items"]}

    hybrid = await post_ok(
        http, "/search", {"q": "bearing", "query_vector": unit_vector(1), "kinds": ["Part"]}
    )
    assert hybrid["mode"] == "hybrid"
    assert hybrid["items"][0]["key"] in {"P-10001", "P-10002"}

    assert {hit["match"] for hit in lexical["items"]} == {"all"}
    # A descriptive query: no Part holds every word, so Parts holding some of them follow,
    # most terms first.
    described = {"q": "tapered roller bearing for the kiln", "kinds": ["Part"]}
    some = await post_ok(http, "/search", described)
    assert some["items"][0]["key"] == "P-10001"
    assert {hit["match"] for hit in some["items"]} == {"partial"}
    assert "roller" in (some["items"][0]["snippet"] or "").lower()
    active = {r["part_number"] for r in rows("part.csv") if r["lifecycle"] == "active"}
    filtered_some = await post_ok(
        http,
        "/search",
        {**described, "where": {"attr": "lifecycle", "op": "eq", "value": "active"}},
    )
    assert filtered_some["items"][0]["key"] == "P-10001"
    assert {hit["key"] for hit in filtered_some["items"]} <= active
    assert {hit["match"] for hit in filtered_some["items"]} == {"partial"}
    filtered_full = await post_ok(
        http,
        "/search",
        {
            "q": "roller bearing",
            "kinds": ["Part"],
            "where": {"attr": "lifecycle", "op": "eq", "value": "active"},
        },
    )
    assert filtered_full["items"]
    assert {hit["match"] for hit in filtered_full["items"]} == {"all"}
    hybrid_some = await post_ok(
        http,
        "/search",
        {"q": "tapered roller kiln", "kinds": ["Part"], "query_vector": unit_vector(40)},
    )
    matches = {hit["key"]: hit["match"] for hit in hybrid_some["items"]}
    assert matches["P-10001"] == "partial"
    assert None in matches.values()

    wrong = await http.post(f"{BASE}/search", json={"query_vector": [0.1, 0.2]})
    assert wrong.status_code == 422
    assert wrong.json()["errors"][0]["code"] == "dimension_mismatch"
    empty = await http.post(f"{BASE}/search", json={"kinds": ["Part"]})
    assert empty.status_code == 422


# Revisions, log and diff ------------------------------------------------------------------


async def test_log_revision_counts_and_changeset(http: httpx.AsyncClient, seeded: Seeded) -> None:
    revisions = seeded.revisions
    log = await get_ok(http, "/branches/main/log")
    assert [item["id"] for item in log["items"]] == [
        revisions["updated"],
        revisions["data"],
        revisions["schema"],
        revisions["created"],
    ]
    paged: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        page = await get_ok(http, "/branches/main/log", **params)
        paged.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert paged == [item["id"] for item in log["items"]]
    imports = await get_ok(http, "/branches/main/log", origin_type="import")
    assert [i["id"] for i in imports["items"]] == [revisions["data"], revisions["schema"]]
    by_author = await get_ok(http, "/branches/feature/risk/log", author="usr_editor")
    assert [i["id"] for i in by_author["items"]] == [revisions["feature"], revisions["updated"]]

    revision = await get_ok(http, f"/revisions/{revisions['updated']}")
    assert revision["parents"] == [revisions["data"]]
    assert revision["origin"]["type"] == "operation"
    assert (await get_ok(http, "/revisions/main"))["id"] == revisions["updated"]

    counts = await get_ok(http, f"/revisions/{revisions['data']}/counts")
    by_name = {c["name"]: c["count"] for c in counts["partitions"]}
    assert by_name["Supplier"] == len(rows("supplier.csv"))
    assert by_name["supplied_by"] == len(rows("supplied_by.csv"))

    changeset = await get_ok(http, f"/revisions/{revisions['updated']}/changeset")
    assert changeset["total"] == 1
    (change,) = changeset["items"]
    assert change["kind"] == "Supplier"
    assert change["change"] == "modified"
    assert change["changes"] == [{"name": "risk_score", "old": "32", "new": "90"}]
    assert set(change["provenance"]) == {"risk_score"}


async def test_diff_between_revisions(http: httpx.AsyncClient, seeded: Seeded) -> None:
    revisions = seeded.revisions
    linear = await get_ok(http, "/diff", **{"from": revisions["data"], "to": revisions["updated"]})
    changeset = await get_ok(http, f"/revisions/{revisions['updated']}/changeset")
    assert [c | {"provenance": None} for c in linear["items"]] == [
        c | {"provenance": None} for c in changeset["items"]
    ]

    full = await get_ok(
        http,
        "/diff",
        **{"from": revisions["schema"], "to": "main", "kinds": "Supplier,Part", "limit": 10},
    )
    summary = {s["name"]: s for s in full["summary"]}
    assert summary["Supplier"]["added"] == len(rows("supplier.csv"))
    assert summary["Part"]["added"] == len(rows("part.csv"))
    assert full["total"] == len(rows("supplier.csv")) + len(rows("part.csv"))
    collected = list(full["items"])
    cursor = full["next_cursor"]
    while cursor:
        page = await get_ok(
            http,
            "/diff",
            **{
                "from": revisions["schema"],
                "to": "main",
                "kinds": "Supplier,Part",
                "limit": 10,
                "cursor": cursor,
            },
        )
        collected.extend(page["items"])
        cursor = page["next_cursor"]
    assert len(collected) == full["total"]
    added = next(c for c in collected if c["key"] == "S-1001")
    assert {v["name"]: v["new"] for v in added["changes"]}["risk_score"] == "90"

    branches = await get_ok(http, "/diff", **{"from": "main", "to": "feature/risk"})
    assert [(c["kind"], c["key"], c["change"]) for c in branches["items"]] == [
        ("Supplier", "S-9999", "added")
    ]
    # materialized counts difference matches the diff summary
    main_count = (await get_ok(http, "/revisions/main/counts"))["partitions"]
    branch_count = (await get_ok(http, f"/revisions/{revisions['feature']}/counts"))["partitions"]
    difference = {c["name"]: c["count"] for c in branch_count}["Supplier"] - {
        c["name"]: c["count"] for c in main_count
    }["Supplier"]
    assert difference == branches["summary"][0]["added"]

    streamed = await http.get(
        f"{BASE}/diff",
        params={"from": revisions["schema"], "to": "main", "kinds": "Supplier", "format": "ndjson"},
    )
    assert streamed.status_code == 200
    lines = [json.loads(line) for line in streamed.text.splitlines() if line]
    assert lines[0]["type"] == "summary"
    assert len(lines) == 1 + len(rows("supplier.csv"))

    schema_diff = await get_ok(
        http, "/diff", **{"from": revisions["created"], "to": revisions["schema"]}
    )
    assert schema_diff["summary"][0]["element"] == "schema"
    kinds = {c["api_name"] for c in schema_diff["items"] if c["element_type"] == "kind"}
    assert {"Supplier", "Part"} <= kinds

    assert (
        await http.get(f"{BASE}/diff", params={"from": "nope", "to": "main"})
    ).status_code == 404
    too_big = await http.get(
        f"{BASE}/diff",
        params={"from": revisions["schema"], "to": "main"},
    )
    assert too_big.status_code == 200
    async with keeper(seeded.app_url, read={"diff": {"max_changes": 5}}) as limited:
        refused = await limited.get(
            f"{BASE}/diff", params={"from": revisions["schema"], "to": "main"}
        )
    assert refused.status_code == 422
    assert refused.json()["errors"][0]["code"] == "diff_too_large"


async def test_reads_on_another_branch(http: httpx.AsyncClient, seeded: Seeded) -> None:
    on_branch = await get_ok(http, "/kinds/Supplier/records/S-9999", branch="feature/risk")
    assert on_branch["revision"] == seeded.revisions["feature"]
    assert (await http.get(f"{BASE}/kinds/Supplier/records/S-9999")).status_code == 404
    assert (
        await http.get(f"{BASE}/kinds/Supplier/records/S-9999", params={"branch": "nope"})
    ).status_code == 404
