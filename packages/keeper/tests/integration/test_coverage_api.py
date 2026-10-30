"""The `coverage` block of `POST /query` and `POST /resolve` (design 8.11) and the recording of
resolution misses (design 8.10), against the retail Domain in Postgres: missing values of the
Attributes used, masked Attributes and Kind-narrowed callers, other branches, statistics older
than the head, and which misses are kept."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.infer.suggestions import Observation, SuggestionOptions, SuggestionType
from lorebase_core.model import Record, Schema
from lorebase_core.validate import RecordUpsert
from lorebase_keeper.auth.api_keys import ApiKeyCodec
from lorebase_keeper.auth.config import ApiKeySettings
from lorebase_store import coverage, values
from lorebase_store.auth import SqlAuthStore
from lorebase_store.db import create_engine
from lorebase_store.materialize import BranchMode, create_branch
from lorebase_store.models.enums import GrantScope, PrincipalType
from lorebase_store.repositories import SchemaSuggestionRepository, scope_to_domain
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import CoverageSettings, DatabaseSettings, StoreSettings

from .read_fixtures import Seeded, keeper, owner_session, seeded_database
from .resolve_shop import DOMAIN, commit_changes, customers, load_shop, products
from .supply import Clock

pytestmark = pytest.mark.integration

BASE = f"/api/v1/domains/{DOMAIN}"
PEPPER = "coverage-tests-pepper-0123456789-012345"
CODEC = ApiKeyCodec(ApiKeySettings(pepper=PEPPER))
SECTION = {
    "auth": {
        "mode": "api_key",
        "api_keys": {"pepper": PEPPER},
        "authz": {"custom_roles": {"suggestion-reader": ["document.read", "schema.read"]}},
    }
}
OVERLAY = "agent/coverage"
RECORDING = {"resolve_misses": {"enabled": True, "caller_key": "k" * 32}}


def without(schema: Schema, changes: list[RecordUpsert], kind: str, name: str) -> list[Any]:
    """`changes` with the Attribute `name` left out of every Record."""
    owner = schema.kind_by_api_name(kind)
    assert owner is not None
    found = owner.attribute_by_api_name(name)
    assert found is not None
    return [
        RecordUpsert(
            record=Record(
                kind=c.record.kind,
                key=c.record.key,
                values={k: v for k, v in c.record.values.items() if k != found.id},
            )
        )
        for c in changes
    ]


async def _key(store: SqlAuthStore, name: str, role: str, grants: list[dict[str, Any]]) -> str:
    async with store.unit() as unit:
        principal = await unit.principals.add(PrincipalType.SERVICE, name)
        for grant in grants:
            given = dict(grant)
            await unit.grants.add(
                domain_id=DOMAIN,
                subject=principal.id,
                role=given.pop("role", role),
                created_by="usr_seed",
                **given,
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
    schema, _ = await load_shop(session, runtime, clock, product_count=300)
    async with owner_session(owner_url) as owner:
        await commit_changes(
            owner,
            runtime,
            clock,
            [
                *without(schema, products(schema, 100, start=300), "Product", "color"),
                *without(schema, customers(schema, 80)[60:], "Customer", "segment"),
            ],
        )
    async with owner_session(owner_url) as owner, owner.begin():
        await scope_to_domain(owner, DOMAIN)
        await values.sync_selection(owner, DOMAIN, settings=runtime.settings)
    async with owner_session(owner_url) as owner:
        await create_branch(
            owner,
            DOMAIN,
            OVERLAY,
            created_by="usr_seed",
            source="main",
            mode=BranchMode.OVERLAY,
            runtime=runtime,
        )
    engine = create_engine(DatabaseSettings(), url=owner_url, pooled=False)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        report = await coverage.run_coverage(sessions, DOMAIN, settings=CoverageSettings())
        assert report.counted, report.as_dict()
        store = SqlAuthStore(sessions)
        keys = {
            "admin": await _key(store, "admin", "admin", [{}]),
            "viewer": await _key(store, "viewer", "viewer", [{}]),
            "products": await _key(
                store,
                "product reader",
                "viewer",
                [{"scope_type": GrantScope.KIND, "scope_ref": "Product"}],
            ),
            # Reads suggestions Domain-wide, Records of Product only.
            "reviewer": await _key(
                store,
                "product reviewer",
                "viewer",
                [
                    {"role": "suggestion-reader"},
                    {"scope_type": GrantScope.KIND, "scope_ref": "Product"},
                ],
            ),
        }
    finally:
        await engine.dispose()
    seeded = Seeded(owner_url=owner_url, app_url=app_url, schema=schema)
    seeded.facts = {"keys": keys}
    return seeded


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


async def _post(
    http: httpx.AsyncClient, seeded: Seeded, path: str, body: dict[str, Any], who: str = "admin"
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {seeded.facts['keys'][who]}"}
    response = await http.post(f"{BASE}/{path}", json=body, headers=headers)
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


def _attributes(block: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(a["kind"], a["attribute"]): a for a in block["attributes"]}


async def test_query_reports_missing_values_of_the_attributes_used(seeded: Seeded) -> None:
    query = {
        "kind": "Product",
        "where": {"attr": "color", "op": "eq", "value": "Rosso"},
        "select": ["name", "color"],
        "order_by": [{"attr": "price"}],
        "limit": 5,
    }
    async with keeper(seeded.app_url, SECTION) as http:
        body = await _post(http, seeded, "query", query)
        off = await _post(http, seeded, "query", {**query, "coverage": False})
    block = body["coverage"]
    assert block["kinds"][0]["kind"] == "Product"
    assert block["kinds"][0]["records"] == 400
    assert block["kinds"][0]["approximate"] is False
    color = _attributes(block)[("Product", "color")]
    assert (color["missing"], color["missing_records"]) == (0.25, 100)
    assert color["used_in"] == ["where", "select"]
    assert _attributes(block)[("Product", "price")]["missing"] == 0
    assert "color missing on 25% of Product: a filter on color excludes them" in block["notes"]
    assert block["omitted"] is None
    assert off["coverage"] is None


async def test_masked_attributes_and_kind_scope_stay_out_of_coverage(seeded: Seeded) -> None:
    query = {"kind": "Customer", "select": ["number", "segment", "loyalty"], "limit": 1}
    async with keeper(seeded.app_url, SECTION) as http:
        admin = await _post(http, seeded, "query", query)
        viewer = await _post(http, seeded, "query", query, "viewer")
        narrowed = await _post(
            http, seeded, "resolve", {"text": "gold customers rosso"}, "products"
        )
    assert _attributes(admin["coverage"])[("Customer", "segment")]["missing"] == 0.25
    seen = _attributes(viewer["coverage"])
    assert ("Customer", "segment") not in seen
    assert ("Customer", "loyalty") in seen
    assert "segment" not in " ".join(viewer["coverage"]["notes"])
    block = narrowed["coverage"]
    assert {k["kind"] for k in block["kinds"]} == {"Product"}
    assert all(a["kind"] == "Product" for a in block["attributes"])


async def test_resolution_lists_unknown_phrases(seeded: Seeded) -> None:
    async with keeper(seeded.app_url, SECTION) as http:
        body = await _post(http, seeded, "resolve", {"text": "zzqx shoes in red"})
        off = await _post(http, seeded, "resolve", {"text": "zzqx", "coverage": False})
    block = body["coverage"]
    assert block["unknown"] == ["zzqx", "in"]
    assert (
        "'zzqx' matched nothing you can read in this Domain (no Kind, Attribute or value)"
        in block["notes"]
    )
    assert ("Product", "color") in _attributes(block)
    assert off["coverage"] is None


async def test_other_branches_get_no_statistics(seeded: Seeded) -> None:
    query = {"kind": "Product", "where": {"attr": "color", "op": "eq", "value": "Rosso"}}
    async with keeper(seeded.app_url, SECTION) as http:
        body = await _post(http, seeded, "query", {**query, "branch": OVERLAY})
    block = body["coverage"]
    assert block["omitted"] == "statistics cover the default branch head only"
    assert block["kinds"] == []
    assert block["attributes"] == []


async def test_misses_are_recorded_only_where_allowed(seeded: Seeded) -> None:
    async with keeper(seeded.app_url, SECTION, store=RECORDING) as http:
        await _post(http, seeded, "resolve", {"text": "zzqx bags"})
        await _post(http, seeded, "resolve", {"text": "zzqx"})
        await _post(http, seeded, "resolve", {"text": "zzqx"}, "viewer")
        await _post(http, seeded, "resolve", {"text": "qqvv", "record_misses": False})
        # A narrowed search may miss what exists elsewhere: not recorded.
        await _post(http, seeded, "resolve", {"text": "qqvv", "levels": ["exact"]})
        await _post(http, seeded, "resolve", {"text": "qqvv", "kinds": ["Product"]})
    narrow = {"resolve": {"misses": {"permission": "schema.write"}}}
    async with keeper(seeded.app_url, SECTION, store=RECORDING, search=narrow) as http:
        await _post(http, seeded, "resolve", {"text": "wwkk"}, "viewer")
        await _post(http, seeded, "resolve", {"text": "wwkk"}, "products")
        await _post(http, seeded, "resolve", {"text": "xxjj"})
    async with owner_session(seeded.owner_url) as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        found = {
            phrase: (misses, callers)
            for phrase, misses, callers in (
                await session.execute(
                    text(
                        "SELECT phrase, misses, cardinality(callers) FROM resolve_misses "
                        "WHERE domain_id = :d AND branch = 'main'"
                    ),
                    {"d": DOMAIN},
                )
            ).all()
        }
    assert found == {"zzqx": (3, 2), "xxjj": (1, 1)}


async def test_statistics_older_than_the_head_are_approximate(seeded: Seeded) -> None:
    async with owner_session(seeded.owner_url) as owner:
        await commit_changes(
            owner,
            StoreRuntime.from_settings(StoreSettings()),
            Clock(),
            products(seeded.schema, 1, start=900),
        )
    query = {"kind": "Product", "where": {"attr": "color", "op": "eq", "value": "Rosso"}}
    async with keeper(seeded.app_url, SECTION) as http:
        body = await _post(http, seeded, "query", query)
    assert body["coverage"]["kinds"][0]["approximate"] is True
    assert any("(approximate)" in note for note in body["coverage"]["notes"])


async def test_vocabulary_suggestions_are_withheld_outside_the_reviewers_kinds(
    seeded: Seeded,
) -> None:
    """Design 8.10: a reviewer who may not read a Kind's Records sees neither the phrases nor
    the values of its vocabulary suggestions, and cannot accept them."""
    product = seeded.schema.kind_by_api_name("Product")
    customer = seeded.schema.kind_by_api_name("Customer")
    assert product is not None
    assert customer is not None
    color = product.attribute_by_api_name("color")
    assert color is not None

    def seen(type_: SuggestionType, phrase: str, kind: str, target: str | None) -> Observation:
        key = f"miss:main:{phrase}"
        return Observation(
            type_,
            phrase,
            key,
            0.6,
            "resolve_misses",
            kind=kind,
            target=target,
            payload={"value": "Rosso"} if target else None,
            weight=3,
            evidence={"document_id": key, "phrase": phrase, "misses": 3},
        )

    async with owner_session(seeded.owner_url) as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await SchemaSuggestionRepository(session, DOMAIN).record(
            [
                seen(SuggestionType.VALUE_ALIAS, "scarlet", product.id, color.id),
                seen(SuggestionType.KIND_ALIAS, "patron", customer.id, None),
            ],
            job_id="vocabulary",
            options=SuggestionOptions(),
            now=Clock()(),
            new_id=lambda: f"ssg_{uuid.uuid4().hex}",
        )
    suggestions = f"{BASE}/schema/suggestions"
    async with keeper(seeded.app_url, SECTION) as http:
        headers = {"Authorization": f"Bearer {seeded.facts['keys']['reviewer']}"}
        listed = await http.get(suggestions, params={"all": "true"}, headers=headers)
        assert listed.status_code == 200, listed.text
        items = {i["type"]: i for i in listed.json()["items"]}
        admin = await http.get(
            suggestions,
            params={"all": "true"},
            headers={"Authorization": f"Bearer {seeded.facts['keys']['admin']}"},
        )
        refused = await http.post(
            f"{suggestions}:decide",
            json={"decisions": [{"suggestion": items["kind_alias"]["id"], "decision": "accepted"}]},
            headers=headers,
        )
    visible = items["value_alias"]
    assert (visible["name"], visible["value"], visible["withheld"]) == ("scarlet", "Rosso", False)
    hidden = items["kind_alias"]
    assert hidden["withheld"] is True
    assert hidden["name"] == "(withheld)"
    assert hidden["labels"] == []
    assert hidden["examples"] == []
    assert "patron" not in listed.text
    assert "patron" in admin.text
    assert refused.status_code in (403, 422), refused.text
    assert "patron" not in refused.text
