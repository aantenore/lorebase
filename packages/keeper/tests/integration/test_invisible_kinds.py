"""Kind-scoped invisibility (design 13.3): a caller whose grants are narrowed to Kinds never
learns of the others.

The reader here holds `viewer` on the Kinds `Part` and `Plant` only. Every request naming a
Kind it may not read (`Supplier`), or a Relation reaching one (`supplied_by`), must answer
exactly like the same request naming something that does not exist; responses must not hold
their names, ids, keys, titles or counts; cached responses and their tags must not cross
between callers who see different Kinds.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.expr.visibility import element_ids, visible_schema
from lorebase_keeper.auth.api_keys import ApiKeyCodec
from lorebase_keeper.auth.config import ApiKeySettings
from lorebase_store.auth import SqlAuthStore
from lorebase_store.db import create_engine
from lorebase_store.models.enums import GrantScope, PrincipalType
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings

from .read_fixtures import Seeded, keeper, seeded_database
from .supply import DOMAIN, Clock, load_supply, rows

pytestmark = pytest.mark.integration

BASE = f"/api/v1/domains/{DOMAIN}"
PEPPER = "invisible-kinds-pepper-0123456789-0123"
CODEC = ApiKeyCodec(ApiKeySettings(pepper=PEPPER))
VISIBLE = frozenset({"Part", "Plant"})
ORDERS = frozenset({"PurchaseOrder", "Part", "Plant"})
"""A reader of purchase orders, which reference Suppliers (`supplier`, `issued_to`)."""
VOLATILE = ("instance", "request_id", "trace_id")
"""Problem members that differ between any two requests."""


async def _principal(store: SqlAuthStore, name: str, grants: list[dict[str, Any]]) -> str:
    async with store.unit() as unit:
        principal = await unit.principals.add(PrincipalType.SERVICE, name)
        for grant in grants:
            await unit.grants.add(
                domain_id=DOMAIN,
                subject=principal.id,
                role="viewer",
                created_by="usr_seed",
                **grant,
            )
        generated = CODEC.generate()
        await unit.api_keys.add(
            principal_id=principal.id,
            prefix=generated.prefix,
            key_hash=generated.key_hash,
            scopes=[f"domain:{DOMAIN}"],
        )
    return generated.token


async def seed(
    session: AsyncSession, runtime: StoreRuntime, clock: Clock, owner_url: str, app_url: str
) -> Seeded:
    schema, _ = await load_supply(session, runtime, clock)
    engine = create_engine(DatabaseSettings(), url=owner_url, pooled=False)
    try:
        store = SqlAuthStore(async_sessionmaker(engine, expire_on_commit=False))
        domain_key = await _principal(store, "domain reader", [{}])
        limited_key = await _principal(
            store,
            "part reader",
            [{"scope_type": GrantScope.KIND, "scope_ref": kind} for kind in sorted(VISIBLE)],
        )
        orders_key = await _principal(
            store,
            "order reader",
            [{"scope_type": GrantScope.KIND, "scope_ref": kind} for kind in sorted(ORDERS)],
        )
    finally:
        await engine.dispose()
    seeded = Seeded(owner_url=owner_url, app_url=app_url, schema=schema)
    seeded.facts = {"domain_key": domain_key, "limited_key": limited_key, "orders_key": orders_key}
    return seeded


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


def _section(invisible: bool = True) -> dict[str, Any]:
    return {
        "auth": {
            "mode": "api_key",
            "api_keys": {"pepper": PEPPER},
            "authz": {"invisible_kinds": invisible},
        }
    }


def _hidden_ids(seeded: Seeded) -> set[str]:
    schema = seeded.schema
    return element_ids(schema) - element_ids(visible_schema(schema, VISIBLE))


class Reader:
    """Requests of one caller; `same_as_unknown` compares a request naming something
    invisible with the same request naming something that does not exist."""

    def __init__(self, http: httpx.AsyncClient, key: str) -> None:
        self.http = http
        self.headers = {"Authorization": f"Bearer {key}"}

    async def get(self, path: str, **params: Any) -> httpx.Response:
        return await self.http.get(BASE + path, params=params, headers=self.headers)

    async def post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        return await self.http.post(BASE + path, json=body, headers=self.headers)

    async def same_as_unknown(
        self, hidden: str, unknown: str, path: str, body: dict[str, Any] | None = None
    ) -> httpx.Response:
        """The request with `hidden` in it answers like the one with `unknown` instead, word
        for word but the name itself."""

        async def send(name: str) -> httpx.Response:
            if body is None:
                return await self.get(path.format(name=name))
            named = json.loads(json.dumps(body).replace("{name}", name))
            return await self.post(path.format(name=name), named)

        seen, missing = await send(hidden), await send(unknown)
        assert seen.status_code == missing.status_code, (seen.text, missing.text)
        assert seen.status_code in (404, 422), seen.text
        assert _problem(seen) == _problem(missing, (unknown, hidden))
        return seen


def _problem(response: httpx.Response, rename: tuple[str, str] | None = None) -> Any:
    text = response.text
    if rename is not None:
        text = text.replace(rename[0], rename[1])
    body = json.loads(text)
    return {k: v for k, v in body.items() if k not in VOLATILE}


async def test_names_of_invisible_kinds_answer_like_unknown_names(seeded: Seeded) -> None:
    async with keeper(seeded.app_url, _section()) as http:
        reader = Reader(http, seeded.facts["limited_key"])
        unknown = "Nonexistent"
        await reader.same_as_unknown("Supplier", unknown, "/kinds/{name}/records")
        await reader.same_as_unknown("Supplier", unknown, "/kinds/{name}/records/S-1005")
        await reader.same_as_unknown("Supplier", unknown, "/kinds/{name}/records/S-1005/history")
        await reader.same_as_unknown("Supplier", unknown, "/kinds/{name}/records/S-1005/lineage")
        await reader.same_as_unknown("Supplier", unknown, "/kinds/{name}/records/S-1005/explain")
        await reader.same_as_unknown("Supplier", unknown, "/kinds/{name}/health")
        await reader.same_as_unknown("Supplier", unknown, "/query", {"kind": "{name}"})
        await reader.same_as_unknown(
            "Supplier", unknown, "/search", {"q": "bearing", "kinds": ["{name}"]}
        )
        await reader.same_as_unknown(
            "Supplier", unknown, "/neighborhood", {"seeds": [{"kind": "{name}", "key": "S-1"}]}
        )
        await reader.same_as_unknown(
            "Supplier",
            unknown,
            "/traverse",
            {"start": {"kind": "{name}", "key": "S-1"}, "path": []},
        )
        await reader.same_as_unknown("Supplier", unknown, "/context", {"refs": ["{name}/S-1005"]})
        await reader.same_as_unknown("Supplier", unknown, "/schema/kinds/{name}")


async def test_relations_reaching_invisible_kinds_answer_like_unknown_ones(seeded: Seeded) -> None:
    async with keeper(seeded.app_url, _section()) as http:
        reader = Reader(http, seeded.facts["limited_key"])
        unknown = "no_such_link"
        hop = "supplied_by"
        await reader.same_as_unknown(hop, unknown, "/kinds/Part/records/P-10001/links/{name}")
        await reader.same_as_unknown(hop, unknown, "/relations/{name}/links")
        await reader.same_as_unknown(hop, unknown, "/schema/relations/{name}")
        part = {"kind": "Part"}
        await reader.same_as_unknown(hop, unknown, "/query", {**part, "where": {"via": "{name}"}})
        count = {"count": "{name}", "op": "gte", "value": 1}
        await reader.same_as_unknown(hop, unknown, "/query", {**part, "where": count})
        include = {**part, "include": {"{name}": {"limit": 1}}}
        await reader.same_as_unknown(hop, unknown, "/query", include)
        start = {"kind": "Part", "key": "P-10001"}
        await reader.same_as_unknown(
            hop, unknown, "/traverse", {"start": start, "path": [{"relation": "{name}"}]}
        )
        await reader.same_as_unknown(
            hop, unknown, "/neighborhood", {"seeds": [start], "relations": ["{name}"]}
        )
        await reader.same_as_unknown(
            hop, unknown, "/context", {"refs": ["Part/P-10001"], "relations": ["{name}"]}
        )
        # Attributes left out with them (a count of Suppliers) are unknown fields.
        await reader.same_as_unknown(
            "supplier_count",
            "no_such_field",
            "/query",
            {**part, "where": {"attr": "{name}", "op": "gte", "value": 1}},
        )


async def test_the_schema_holds_only_what_the_caller_may_read(seeded: Seeded) -> None:
    hidden = _hidden_ids(seeded)
    assert hidden
    async with keeper(seeded.app_url, _section()) as http:
        reader = Reader(http, seeded.facts["limited_key"])
        schema = await reader.get("/schema")
        assert schema.status_code == 200, schema.text
        document = schema.json()["schema"]
        assert {k["api_name"] for k in document["kinds"].values()} == VISIBLE
        assert [r["api_name"] for r in document["relations"].values()] == ["stocked_at"]
        assert not [i for i in hidden if i in schema.text]
        yaml_body = await reader.get("/schema", format="yaml")
        assert yaml_body.status_code == 200
        assert "Supplier:" not in yaml_body.text
        assert "supplied_by" not in yaml_body.text
        # The Domain, its branches and tags, which the web app needs to find its way.
        assert (await reader.get("")).status_code == 200
        branches = await reader.get("/branches")
        assert branches.status_code == 200
        assert all(b["materialization_progress"] is None for b in branches.json()["items"])
        assert (await reader.get("/branches/main")).status_code == 200
        assert (await reader.get("/tags")).status_code == 200
        listed = await http.get("/api/v1/domains", headers=reader.headers)
        assert DOMAIN in {d["id"] for d in listed.json()["items"]}
        # History is not limited to Kinds: refused as a whole, naming no Kind.
        log = await reader.get("/branches/main/log")
        assert log.status_code == 403
        assert "Supplier" not in log.text


async def test_reads_leave_out_invisible_kinds_and_their_counts(seeded: Seeded) -> None:
    hidden = _hidden_ids(seeded)
    async with keeper(seeded.app_url, _section()) as http:
        wide = Reader(http, seeded.facts["domain_key"])
        reader = Reader(http, seeded.facts["limited_key"])
        record = "/kinds/Part/records/P-10001"
        # A wider caller's response for the same request is cached first.
        full = await wide.get(record, links="true")
        assert full.status_code == 200
        assert "supplier_count" in full.json()["record"]["values"]
        seen = await reader.get(record, links="true")
        assert seen.status_code == 200, seen.text
        assert seen.headers["etag"] != full.headers["etag"]
        values = seen.json()["record"]["values"]
        assert "supplier_count" not in values
        assert "single_sourced" not in values
        relations = {c["relation"] for c in seen.json()["record"]["links"]}
        assert relations <= {"stocked_at"}
        assert not [i for i in hidden if i in seen.text]
        # Revalidating with the wider caller's tag does not answer 304.
        again = await http.get(
            BASE + record,
            params={"links": "true"},
            headers={**reader.headers, "If-None-Match": full.headers["etag"]},
        )
        assert again.status_code == 200

        # Neighborhoods, traversals and context packs never reach a Supplier, and their
        # `truncated` flags count only what is visible.
        hood = await reader.post(
            "/neighborhood", {"seeds": [{"kind": "Part", "key": "P-10001"}], "depth": 2}
        )
        assert hood.status_code == 200
        assert {n["kind"] for n in hood.json()["nodes"]} <= VISIBLE
        assert {e["relation"] for e in hood.json()["edges"]} <= {"stocked_at"}
        assert "Supplier" not in hood.text
        pack = await reader.post("/context", {"refs": ["Part/P-10001"], "budget_tokens": 32000})
        assert pack.status_code == 200, pack.text
        assert "Supplier" not in pack.text
        assert "supplied_by" not in pack.text
        assert not [i for i in hidden if i in pack.text]

        # Search over every Kind the caller may read finds no Supplier, and words held by
        # Suppliers only find nothing.
        found = await reader.post("/search", {"q": "Kavsen Aloys"})
        assert found.status_code == 200
        assert found.json()["items"] == []
        bearing = await reader.post("/search", {"q": "bearing"})
        assert {i["kind"] for i in bearing.json()["items"]} <= VISIBLE

        # Health covers the visible Kinds and Relations only; totals count nothing else.
        health = await reader.get("/health")
        assert health.status_code == 200, health.text
        report = health.json()
        assert {k["kind"] for k in report["kinds"]} == VISIBLE
        assert {r["relation"] for r in report["relations"]} == {"stocked_at"}
        assert report["summary"]["records"] == sum(k["records"] for k in report["kinds"])
        assert not [i for i in hidden if i in health.text]


async def test_the_setting_off_refuses_invisible_kinds_with_403(seeded: Seeded) -> None:
    async with keeper(seeded.app_url, _section(invisible=False)) as http:
        reader = Reader(http, seeded.facts["limited_key"])
        assert (await reader.get("/kinds/Supplier/records")).status_code == 403
        assert (await reader.get("/schema")).status_code == 403
        assert (await reader.get("")).status_code == 403
        assert (await reader.get("/kinds/Part/records/P-10001")).status_code == 200


async def test_references_to_invisible_kinds_are_left_out_of_visible_records(
    seeded: Seeded,
) -> None:
    schema = seeded.schema
    hidden = element_ids(schema) - element_ids(visible_schema(schema, ORDERS))
    order = rows("purchase_order.csv")[0]["po_number"]
    async with keeper(seeded.app_url, _section()) as http:
        reader = Reader(http, seeded.facts["orders_key"])
        record = await reader.get(f"/kinds/PurchaseOrder/records/{order}", links="true")
        assert record.status_code == 200, record.text
        assert "supplier" not in record.json()["record"]["values"]
        assert "issued_to" not in {c["relation"] for c in record.json()["record"]["links"]}
        assert "S-1" not in record.text  # no Supplier key
        await reader.same_as_unknown(
            "supplier",
            "no_such_field",
            "/query",
            {"kind": "PurchaseOrder", "where": {"attr": "{name}", "op": "eq", "value": "S-1001"}},
        )
        await reader.same_as_unknown("issued_to", "no_such_link", "/relations/{name}/links")
        history = await reader.get(f"/kinds/PurchaseOrder/records/{order}/history")
        assert history.status_code == 200
        assert "S-1" not in history.text
        assert not [i for i in hidden if i in history.text]
        health = await reader.get("/kinds/PurchaseOrder/health")
        assert health.status_code == 200, health.text
        assert not [i for i in hidden if i in health.text]
        assert health.json()["health"]["violations"]["invalid_records"] == 0
