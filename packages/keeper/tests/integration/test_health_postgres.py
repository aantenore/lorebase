"""Domain and Kind health reports against the `supply` example in Postgres.

`main` holds the example as imported. The branch `broken` is a copy of it with rows damaged
behind the application's back (SQL as the database owner), the way a bug or a manual edit would:
invalid and missing values, duplicates, a deleted Record that Links still point at, a deleted
required Link and dangling foreign keys.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_keeper.auth.api_keys import ApiKeyCodec
from lorebase_keeper.auth.config import ApiKeySettings
from lorebase_keeper.kindhealth import report
from lorebase_store.auth import SqlAuthStore
from lorebase_store.db import create_engine
from lorebase_store.materialize import create_branch
from lorebase_store.models.enums import GrantScope, PrincipalType
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings

from .read_fixtures import Seeded, keeper, seeded_database
from .supply import DOMAIN, Clock, load_supply, rows

pytestmark = pytest.mark.integration

BASE = f"/api/v1/domains/{DOMAIN}"
BROKEN = "broken"
PEPPER = "health-pepper-0123456789-0123456789"
CODEC = ApiKeyCodec(ApiKeySettings(pepper=PEPPER))


async def _mutate(session: AsyncSession, statement: str, **params: Any) -> None:
    await session.execute(text(statement), {"d": DOMAIN, "b": BROKEN, **params})


async def _key(store: SqlAuthStore, name: str, **grant: Any) -> str:
    async with store.unit() as unit:
        principal = await unit.principals.add(PrincipalType.SERVICE, name)
        await unit.grants.add(
            domain_id=DOMAIN, subject=principal.id, role="viewer", created_by="usr_seed", **grant
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
    schema, (_, _, data) = await load_supply(session, runtime, clock)
    await create_branch(
        session, DOMAIN, BROKEN, created_by="usr_seed", runtime=runtime, clock=clock
    )

    def kind(name: str) -> Any:
        found = schema.kind_by_api_name(name)
        assert found is not None
        return found

    def attribute(kind_name: str, name: str) -> str:
        found = kind(kind_name).attribute_by_api_name(name)
        assert found is not None
        return found.id

    def relation(name: str) -> Any:
        found = schema.relation_by_api_name(name)
        assert found is not None
        return found[0]

    supplier, plant, order = kind("Supplier").id, kind("Plant").id, kind("PurchaseOrder").id
    legal_name, vat = attribute("Supplier", "legal_name"), attribute("Supplier", "vat_number")
    country, supplier_ref = attribute("Supplier", "country"), attribute("PurchaseOrder", "supplier")
    suppliers = {r["supplier_code"]: r for r in rows("supplier.csv")}
    async with session.begin():
        set_value = (
            'UPDATE mat_records SET "values" = '
            'jsonb_set("values", ARRAY[:a], to_jsonb(CAST(:v AS text))) '
            "WHERE domain_id = :d AND branch = :b AND kind_id = :k AND key = :key"
        )
        # an invalid value, a missing required value
        await _mutate(session, set_value, a=country, v="germany", k=supplier, key="S-1003")
        await _mutate(
            session,
            'UPDATE mat_records SET "values" = "values" - :a '
            "WHERE domain_id = :d AND branch = :b AND kind_id = :k AND key = :key",
            a=legal_name,
            k=supplier,
            key="S-1004",
        )
        # duplicates once case and spacing are ignored: a title and a (sensitive) unique value
        shouty = "  " + suppliers["S-1002"]["legal_name"].upper().replace(" ", "   ") + " "
        await _mutate(session, set_value, a=legal_name, v=shouty, k=supplier, key="S-1005")
        await _mutate(
            session,
            set_value,
            a=vat,
            v=suppliers["S-1002"]["vat_number"].lower(),
            k=supplier,
            key="S-1006",
        )
        # a Record that Links still point at is gone
        await _mutate(
            session,
            "DELETE FROM mat_records WHERE domain_id = :d AND branch = :b AND kind_id = :k "
            "AND key = :key",
            k=plant,
            key="PL-01",
        )
        # a required Link is gone
        await _mutate(
            session,
            "DELETE FROM mat_links WHERE domain_id = :d AND branch = :b AND relation_id = :r "
            "AND from_key = :key",
            r=relation("for_part").id,
            key="PO-200001",
        )
        # a dangling foreign key, and a missing one
        await _mutate(session, set_value, a=supplier_ref, v="S-9999", k=order, key="PO-200002")
        await _mutate(
            session,
            'UPDATE mat_records SET "values" = "values" - :a '
            "WHERE domain_id = :d AND branch = :b AND kind_id = :k AND key = :key",
            a=supplier_ref,
            k=order,
            key="PO-200003",
        )
    engine = create_engine(DatabaseSettings(), url=owner_url, pooled=False)
    try:
        store = SqlAuthStore(async_sessionmaker(engine, expire_on_commit=False))
        supplier_key = await _key(
            store, "supplier reader", scope_type=GrantScope.KIND, scope_ref="Supplier"
        )
        domain_key = await _key(store, "domain reader")
    finally:
        await engine.dispose()
    seeded = Seeded(owner_url=owner_url, app_url=app_url, schema=schema)
    seeded.revisions = {"data": data.revision_id.hex()}
    seeded.facts = {"supplier_key": supplier_key, "domain_key": domain_key}
    return seeded


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


@pytest.fixture
async def http(seeded: Seeded) -> Any:
    async with keeper(seeded.app_url) as client:
        yield client


async def health(http: httpx.AsyncClient, path: str = "/health", **params: Any) -> Any:
    response = await http.get(BASE + path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def by_name(items: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {item[key]: item for item in items}


def count(name: str, **match: str) -> int:
    return sum(1 for r in rows(name) if all(r[k] == v for k, v in match.items()))


# The example as imported ----------------------------------------------------------------------


async def test_the_imported_example_is_sound(http: httpx.AsyncClient, seeded: Seeded) -> None:
    body = await health(http)
    assert body["domain"] == DOMAIN
    assert body["branch"] == "main"
    assert body["revision"] == seeded.revisions["data"]
    assert body["served_from"] == "materialized"
    kinds = by_name(body["kinds"], "kind")
    assert {name: k["records"] for name, k in kinds.items()} == {
        "Supplier": len(rows("supplier.csv")),
        "Part": len(rows("part.csv")),
        "Plant": len(rows("plant.csv")),
        "Contract": len(rows("contract.csv")),
        "PurchaseOrder": len(rows("purchase_order.csv")),
        "RiskFlag": len(rows("risk_flag.csv")),
    }
    summary = body["summary"]
    assert summary["ok"], summary
    assert (summary["invalid_records"], summary["orphan_links"]) == (0, 0)
    assert summary["required_relation_gaps"] == 0
    # same titles are only hints: the example has several parts and plants named alike
    groups = [g for k in kinds.values() for g in k["duplicates"]["groups"]]
    assert groups
    assert not any(g["unique"] for g in groups)
    assert summary["duplicate_groups"] == sum(
        k["duplicates"]["total_groups"] for k in kinds.values()
    )
    for kind in kinds.values():
        violations = kind["violations"]
        assert violations["checked"] == kind["records"]
        assert violations["sampled"] is False
        assert violations["issues"] == []
        assert kind["last_change"]["revision"] == seeded.revisions["data"]
    relations = by_name(body["relations"], "relation")
    assert relations["supplied_by"]["links"] == len(rows("supplied_by.csv"))
    assert relations["for_part"]["links"] == len(rows("for_part.csv"))
    assert relations["for_part"]["required"] is True
    assert relations["for_part"]["required_gaps"] == {"missing": 0, "examples": []}
    assert relations["issued_to"]["backing"] == "foreign_key"
    assert relations["issued_to"]["links"] == len(rows("purchase_order.csv"))
    # the materializer computed the derived Attributes of Part for every Record
    assert kinds["Part"]["derived"] == {
        "attributes": ["supplier_count", "single_sourced"],
        "records_missing_value": 0,
        "stale": False,
    }
    assert kinds["Supplier"]["derived"] is None
    assert summary["stale_kinds"] == 0


# The damaged branch ---------------------------------------------------------------------------


async def test_damage_is_found_per_kind_and_relation(http: httpx.AsyncClient) -> None:
    body = await health(http, branch=BROKEN)
    assert body["branch"] == BROKEN
    summary = body["summary"]
    assert summary["ok"] is False
    kinds = by_name(body["kinds"], "kind")

    supplier = kinds["Supplier"]
    assert supplier["records"] == len(rows("supplier.csv"))
    assert supplier["violations"]["invalid_records"] == 2
    issues = {(i["code"], i["attribute"]): i for i in supplier["violations"]["issues"]}
    assert issues[("value.pattern", "country")]["examples"] == ["S-1003"]
    assert issues[("value.required", "legal_name")]["examples"] == ["S-1004"]
    duplicates = by_name(supplier["duplicates"]["groups"], "attribute")
    assert supplier["duplicates"]["total_groups"] == 2
    assert duplicates["legal_name"]["keys"] == ["S-1002", "S-1005"]
    assert duplicates["legal_name"]["value"].startswith("sundello seals")
    assert duplicates["vat_number"]["unique"] is True
    assert duplicates["legal_name"]["unique"] is False
    assert duplicates["vat_number"]["keys"] == ["S-1002", "S-1006"]
    assert duplicates["vat_number"]["value"] is None  # confidential: never printed

    assert kinds["Plant"]["records"] == len(rows("plant.csv")) - 1
    order = kinds["PurchaseOrder"]
    assert order["violations"]["invalid_records"] == 1
    assert order["violations"]["issues"][0]["examples"] == ["PO-200003"]

    relations = by_name(body["relations"], "relation")
    stocked = count("stocked_at.csv", to_key="PL-01")
    delivered = count("delivered_to.csv", to_key="PL-01")
    assert stocked
    assert delivered
    assert relations["stocked_at"]["orphan_links"]["count"] == stocked
    assert relations["stocked_at"]["orphan_links"]["missing_to"] == stocked
    assert relations["stocked_at"]["orphan_links"]["missing_from"] == 0
    assert relations["delivered_to"]["orphan_links"]["count"] == delivered
    example = relations["stocked_at"]["orphan_links"]["examples"][0]
    assert example["to_key"] == "PL-01"
    assert relations["for_part"]["required_gaps"] == {"missing": 1, "examples": ["PO-200001"]}
    assert relations["for_part"]["links"] == len(rows("for_part.csv")) - 1
    issued = relations["issued_to"]
    assert issued["links"] == len(rows("purchase_order.csv")) - 1
    assert issued["orphan_links"]["count"] == 1
    assert issued["orphan_links"]["examples"] == [{"from_key": "PO-200002", "to_key": "S-9999"}]
    assert issued["required_gaps"] == {"missing": 1, "examples": ["PO-200003"]}

    assert summary["invalid_records"] == 3
    assert summary["orphan_links"] == stocked + delivered + 1
    assert summary["required_relation_gaps"] == 2
    assert summary["duplicate_groups"] == sum(
        k["duplicates"]["total_groups"] for k in kinds.values()
    )


async def test_a_kind_report_has_the_same_findings_and_its_relations(
    http: httpx.AsyncClient,
) -> None:
    domain = by_name((await health(http, branch=BROKEN))["kinds"], "kind")
    body = await health(http, "/kinds/Plant/health", branch=BROKEN)
    assert body["health"]["kind"] == "Plant"
    assert body["health"] == domain["Plant"]
    relations = by_name(body["relations"], "relation")
    assert set(relations) == {"stocked_at", "delivered_to"}
    assert relations["stocked_at"]["direction"] == "incoming"
    assert relations["stocked_at"]["from_kind"] == "Part"
    assert body["summary"]["kinds"] == 1
    assert body["summary"]["orphan_links"] == sum(
        r["orphan_links"]["count"] for r in relations.values()
    )

    order = (await health(http, "/kinds/PurchaseOrder/health", branch=BROKEN))["relations"]
    sides = {r["relation"]: r["direction"] for r in order}
    assert sides == {"issued_to": "outgoing", "for_part": "outgoing", "delivered_to": "outgoing"}


async def test_large_kinds_are_sampled_within_the_cap(seeded: Seeded) -> None:
    section = {"health": {"max_records_validated": 10, "max_duplicate_groups": 1}}
    async with keeper(seeded.app_url, read=section) as http:
        supplier = (await health(http, "/kinds/Supplier/health"))["health"]
    assert supplier["records"] == len(rows("supplier.csv"))
    assert supplier["violations"]["sampled"] is True
    assert 1 <= supplier["violations"]["checked"] <= 10
    async with keeper(seeded.app_url, read=section) as http:
        again = (await health(http, "/kinds/Supplier/health"))["health"]
    assert again == supplier  # the sample is stable

    async with keeper(seeded.app_url, read={"health": {"duplicates_max_records": 5}}) as http:
        skipped = (await health(http, "/kinds/Supplier/health", branch=BROKEN))["health"]
    assert skipped["duplicates"]["skipped"] == "too_many_records"
    assert skipped["duplicates"]["groups"] == []


# Rendering, caching and access ------------------------------------------------------------------


async def test_markdown_is_meant_for_agents(http: httpx.AsyncClient) -> None:
    response = await http.get(f"{BASE}/health", params={"branch": BROKEN, "format": "markdown"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    text_ = response.text
    assert text_.startswith(f"# Health of {DOMAIN} on {BROKEN}\n")
    assert "needs attention" in text_
    assert "- value.pattern on country: 1 (e.g. S-1003)" in text_
    assert "- value.required on legal_name: 1 (e.g. S-1004)" in text_
    assert '- legal_name "sundello seals' in text_
    assert (
        "- vat_number [unique]: S-1002, S-1006 (2)" in text_
    )  # the confidential value is left out
    assert "S-1004" in text_
    assert "PL-01" in text_
    assert "| stocked_at | Part -> Plant |" in text_
    assert "S-1002@" not in text_
    kind = await http.get(
        f"{BASE}/kinds/PurchaseOrder/health", params={"branch": BROKEN, "format": "markdown"}
    )
    assert kind.text.startswith(f"# Health of PurchaseOrder in {DOMAIN} on {BROKEN}\n")
    clean = await http.get(f"{BASE}/health", params={"format": "markdown"})
    assert clean.text.splitlines()[3].startswith("ok: 6 kinds")
    assert "kinds with stale derived values: 0" in clean.text


async def test_a_report_is_computed_once_per_revision(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    original = report.scan_branch

    async def counting(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(report, "scan_branch", counting)
    first = await http.get(f"{BASE}/health", params={"branch": BROKEN})
    second = await http.get(f"{BASE}/health", params={"branch": BROKEN})
    assert calls == 1
    assert second.content == first.content
    etag = first.headers["ETag"]
    assert first.headers["Lorebase-Revision"] == first.json()["revision"]
    revalidated = await http.get(
        f"{BASE}/health", params={"branch": BROKEN}, headers={"If-None-Match": etag}
    )
    assert revalidated.status_code == 304
    assert calls == 1
    await http.get(f"{BASE}/health", params={"branch": BROKEN, "format": "markdown"})
    await http.get(f"{BASE}/kinds/Supplier/health", params={"branch": BROKEN})
    assert calls == 3  # another format and another report are other cache entries


async def test_unknown_names_and_formats_are_refused(http: httpx.AsyncClient) -> None:
    assert (await http.get(f"{BASE}/health", params={"branch": "nope"})).status_code == 404
    assert (await http.get(f"{BASE}/kinds/Nope/health")).status_code == 404
    assert (await http.get("/api/v1/domains/nope/health")).status_code == 404
    refused = await http.get(f"{BASE}/health", params={"format": "xml"})
    assert refused.status_code == 422


async def test_kind_scoped_grants_see_only_their_kinds(seeded: Seeded) -> None:
    auth = {"auth": {"mode": "api_key", "api_keys": {"pepper": PEPPER}}}
    narrow = {"Authorization": f"Bearer {seeded.facts['supplier_key']}"}
    wide = {"Authorization": f"Bearer {seeded.facts['domain_key']}"}
    async with keeper(seeded.app_url, auth) as http:
        mine = await http.get(f"{BASE}/health", params={"branch": BROKEN}, headers=narrow)
        assert mine.status_code == 200, mine.text
        body = mine.json()
        assert [k["kind"] for k in body["kinds"]] == ["Supplier"]
        assert body["relations"] == []  # every Relation of the example reaches another Kind
        assert body["summary"]["records"] == len(rows("supplier.csv"))
        assert body["summary"]["orphan_links"] == 0  # other Kinds' damage is not counted
        text_ = (
            await http.get(
                f"{BASE}/health", params={"branch": BROKEN, "format": "markdown"}, headers=narrow
            )
        ).text
        assert "PurchaseOrder" not in text_
        assert "Plant" not in text_
        own = await http.get(
            f"{BASE}/kinds/Supplier/health", params={"branch": BROKEN}, headers=narrow
        )
        assert own.status_code == 200
        assert own.json()["relations"] == []
        other = await http.get(
            f"{BASE}/kinds/Plant/health", params={"branch": BROKEN}, headers=narrow
        )
        assert other.status_code == 403
        assert (await http.get(f"{BASE}/health", headers={})).status_code == 401

        full = await http.get(f"{BASE}/health", params={"branch": BROKEN}, headers=wide)
        assert full.status_code == 200
        assert len(full.json()["kinds"]) == 6
        # the cache does not hand the full report to the narrow reader, nor the reverse
        again = await http.get(f"{BASE}/health", params={"branch": BROKEN}, headers=narrow)
        assert [k["kind"] for k in again.json()["kinds"]] == ["Supplier"]


def test_a_statement_timeout_is_recognized() -> None:
    from lorebase_keeper.kindhealth.scan import is_timeout

    class CancelledError(Exception):
        sqlstate = "57014"

    class WrapperError(Exception):
        orig = CancelledError("canceling statement due to statement timeout")

    assert is_timeout(WrapperError())
    assert is_timeout(CancelledError())
    assert not is_timeout(ValueError("syntax error"))
