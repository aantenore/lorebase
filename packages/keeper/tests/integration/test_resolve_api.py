"""`POST /resolve` (design 8.9) against a small retail Domain in Postgres: the levels, what
callers narrowed to Kinds or without clearance see, overlays, time travel, and the precision
and recall of the evaluation cases."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.model import Schema
from lorebase_keeper.auth.api_keys import ApiKeyCodec
from lorebase_keeper.auth.config import ApiKeySettings
from lorebase_keeper.reading.settings import SearchSettings
from lorebase_store import values
from lorebase_store.auth import SqlAuthStore
from lorebase_store.db import create_engine
from lorebase_store.embeddings import template_fingerprints
from lorebase_store.materialize import BranchMode, create_branch
from lorebase_store.models.enums import GrantScope, PrincipalType
from lorebase_store.repositories import scope_to_domain
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, StoreSettings

from .read_fixtures import Seeded, keeper, owner_session, seeded_database
from .resolve_shop import (
    CASES,
    DOMAIN,
    commit_changes,
    found,
    load_shop,
    precision_recall,
    products,
)
from .supply import Clock

pytestmark = pytest.mark.integration

BASE = f"/api/v1/domains/{DOMAIN}"
PEPPER = "resolve-tests-pepper-0123456789-0123456"
CODEC = ApiKeyCodec(ApiKeySettings(pepper=PEPPER))
OVERLAY = "agent/repaint"
DIMS = StoreSettings().embeddings.dims
SECTION = {"auth": {"mode": "api_key", "api_keys": {"pepper": PEPPER}}}


async def _key(store: SqlAuthStore, name: str, role: str, grants: list[dict[str, Any]]) -> str:
    async with store.unit() as unit:
        principal = await unit.principals.add(PrincipalType.SERVICE, name)
        for grant in grants:
            await unit.grants.add(
                domain_id=DOMAIN, subject=principal.id, role=role, created_by="usr_seed", **grant
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
    schema, results = await load_shop(session, runtime, clock)
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
    async with owner_session(owner_url) as owner:
        await commit_changes(
            owner, runtime, clock, products(schema, 3, color="Viola"), branch=OVERLAY
        )
    await _embed(owner_url, schema)
    engine = create_engine(DatabaseSettings(), url=owner_url, pooled=False)
    try:
        store = SqlAuthStore(async_sessionmaker(engine, expire_on_commit=False))
        keys = {
            "admin": await _key(store, "admin", "admin", [{}]),
            "viewer": await _key(store, "viewer", "viewer", [{}]),
            "products": await _key(
                store,
                "product reader",
                "viewer",
                [{"scope_type": GrantScope.KIND, "scope_ref": "Product"}],
            ),
        }
    finally:
        await engine.dispose()
    seeded = Seeded(owner_url=owner_url, app_url=app_url, schema=schema)
    seeded.facts = {"keys": keys, "schema_revision": results[1].revision_id.hex()}
    return seeded


def unit_vector(index: int) -> list[float]:
    vector = [0.0] * DIMS
    vector[index % DIMS] = 1.0
    return vector


EMBEDDED = {"P-000001": 0, "P-000002": 5, "P-000004": 10}
"""Products with a vector (index of its unit axis): P-000001 is a red bag."""


async def _embed(owner_url: str, schema: Schema) -> None:
    product = schema.kind_by_api_name("Product")
    assert product is not None
    fingerprint = template_fingerprints([product])[product.id]
    async with owner_session(owner_url) as session, session.begin():
        found = await session.execute(
            text(
                "SELECT key, object_hash FROM mat_records WHERE domain_id = :d "
                "AND branch = 'main' AND kind_id = :k AND key = ANY(:keys)"
            ),
            {"d": DOMAIN, "k": product.id, "keys": list(EMBEDDED)},
        )
        for key, digest in found.all():
            await session.execute(
                text(
                    "INSERT INTO embeddings (domain_id, object_hash, model, fingerprint, dims, "
                    "embedding) VALUES (:d, :h, :m, :f, :n, CAST(:v AS vector))"
                ),
                {
                    "d": DOMAIN,
                    "h": digest,
                    "m": SearchSettings().semantic.model,
                    "f": fingerprint,
                    "n": DIMS,
                    "v": str(unit_vector(EMBEDDED[key])),
                },
            )


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


async def _resolve(
    http: httpx.AsyncClient, seeded: Seeded, body: dict[str, Any], who: str = "admin"
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {seeded.facts['keys'][who]}"}
    return await http.post(f"{BASE}/resolve", json=body, headers=headers)


def _by(candidates: list[dict[str, Any]], kind: str, attribute: str | None, target: str) -> Any:
    return next(
        (
            c
            for c in candidates
            if c["kind"] == kind and c.get("attribute") == attribute and c["target"] == target
        ),
        None,
    )


async def test_grounds_the_words_of_a_question(seeded: Seeded) -> None:
    async with keeper(seeded.app_url, SECTION) as http:
        response = await _resolve(http, seeded, {"text": "average price of red shoes"})
    assert response.status_code == 200, response.text
    body = response.json()
    candidates = body["candidates"]
    color = _by(candidates, "Product", "color", "value")
    assert color["values"] == ["Rosso", "red"]  # 60 each: ties by value
    assert color["level"] == "exact"
    assert color["matched"] == "red"
    assert color["predicate"] == {"attr": "color", "op": "in", "value": ["Rosso", "red"]}
    assert color["records"] == 120
    category = _by(candidates, "Product", "category", "value")
    assert category["values"] == ["Calzature"]
    assert category["level"] == "synonym"
    assert category["predicate"] == {"attr": "category", "op": "eq", "value": "Calzature"}
    assert category["records"] == 100
    price = _by(candidates, "Product", "price", "attribute")
    assert price["type"] == "decimal(10,2)"
    assert price["level"] == "exact"
    assert "average" in body["unmatched"]
    assert {"level": "embedding", "reason": "other levels matched"} in body["skipped"]


async def test_evaluation_cases(seeded: Seeded) -> None:
    results = []
    async with keeper(seeded.app_url, SECTION) as http:
        for case in CASES:
            response = await _resolve(http, seeded, {"text": case.text, "limit": 10})
            assert response.status_code == 200, response.text
            results.append((case, response.json()["candidates"]))
    misses = {
        case.text: sorted(map(str, case.expected - found(candidates)))
        for case, candidates in results
        if case.expected - found(candidates)
    }
    extra = {
        case.text: sorted(map(str, found(candidates) - case.expected))
        for case, candidates in results
        if found(candidates) - case.expected
    }
    precision, recall = precision_recall(results)
    print(f"resolve eval: precision {precision:.2f}, recall {recall:.2f}; extra {extra}")
    assert recall >= 0.95, misses
    assert precision >= 0.75, extra


async def test_misspellings_reach_values_by_similarity(seeded: Seeded) -> None:
    async with keeper(seeded.app_url, SECTION) as http:
        response = await _resolve(http, seeded, {"text": "rossso", "levels": ["trigram"]})
        exact_only = await _resolve(http, seeded, {"text": "rossso", "levels": ["exact"]})
    color = _by(response.json()["candidates"], "Product", "color", "value")
    assert color["level"] == "trigram"
    assert color["values"] == ["Rosso"]
    assert 0 < color["score"] < 1
    assert exact_only.json()["candidates"] == []
    assert exact_only.json()["unmatched"] == ["rossso"]


async def test_callers_see_only_what_they_may_read(seeded: Seeded) -> None:
    async with keeper(seeded.app_url, SECTION) as http:
        admin = await _resolve(http, seeded, {"text": "segment of gold customers in rome"})
        viewer = await _resolve(
            http, seeded, {"text": "segment of gold customers in rome"}, "viewer"
        )
        narrowed = await _resolve(
            http, seeded, {"text": "segment of gold customers in rome"}, "products"
        )
        refused = await _resolve(http, seeded, {"text": "x", "kinds": ["Store"]}, "products")
        sensitive = await _resolve(http, seeded, {"text": "students"})
    assert _by(admin.json()["candidates"], "Customer", "segment", "attribute") is not None
    seen = viewer.json()["candidates"]
    assert _by(seen, "Customer", "segment", "attribute") is None
    assert _by(seen, "Customer", "loyalty", "value") is not None
    limited = narrowed.json()["candidates"]
    assert all(c["kind"] == "Product" for c in limited), limited
    # Invisible Kinds (the default) answer like unknown ones.
    assert refused.status_code == 422
    assert "unknown Kind 'Store'" in refused.text
    # Values of sensitive Attributes are never indexed, cleared or not.
    assert sensitive.json()["candidates"] == []


async def test_overlay_counts_its_own_rows(seeded: Seeded) -> None:
    async with keeper(seeded.app_url, SECTION) as http:
        main = await _resolve(http, seeded, {"text": "viola rosso"})
        overlay = await _resolve(http, seeded, {"text": "viola rosso", "branch": OVERLAY})
    assert _by(main.json()["candidates"], "Product", "color", "value")["values"] == ["Rosso"]
    candidates = overlay.json()["candidates"]
    viola = next(c for c in candidates if c.get("values") == ["Viola"])
    assert viola["records"] == 3
    rosso = next(c for c in candidates if c.get("values") == ["Rosso"])
    assert rosso["records"] == 59  # P-000000 was Rosso and is Viola on the overlay


async def test_time_travel_reads_a_snapshot(seeded: Seeded) -> None:
    revision = seeded.facts["schema_revision"]
    async with keeper(seeded.app_url, SECTION) as http:
        response = await _resolve(http, seeded, {"text": "red shoes", "at": revision})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["revision"] == revision
    # Before the data: names and aliases only, no stored value is counted.
    assert all(c["target"] != "value" or c["records"] in (0, None) for c in body["candidates"])


async def test_bounds_and_embedding_level(seeded: Seeded) -> None:
    async with keeper(seeded.app_url, SECTION) as http:
        long_text = await _resolve(http, seeded, {"text": "x" * 501})
        too_many = await _resolve(http, seeded, {"text": "red", "limit": 101})
        unknown = await _resolve(http, seeded, {"text": "red", "kinds": ["Nothing"]})
        nothing = await _resolve(http, seeded, {"text": "zzqx wvvk"})
        limited = await _resolve(http, seeded, {"text": "red shoes products", "limit": 1})
    assert long_text.status_code == 422
    assert too_many.status_code == 422
    assert unknown.status_code == 422
    body = nothing.json()
    assert body["candidates"] == []
    assert {"level": "embedding", "reason": "no query_vector"} in body["skipped"]
    assert len(limited.json()["candidates"]) == 1


async def test_embedding_level_reads_the_nearest_records(seeded: Seeded) -> None:
    async with keeper(seeded.app_url, SECTION) as http:
        response = await _resolve(
            http, seeded, {"text": "zzqx wvvk", "query_vector": unit_vector(0)}
        )
        disabled = await _resolve(
            http,
            seeded,
            {"text": "zzqx", "query_vector": unit_vector(0), "levels": ["exact", "trigram"]},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["skipped"] == []
    assert body["unmatched"] == []
    candidates = body["candidates"]
    assert candidates
    assert {c["level"] for c in candidates} == {"embedding"}
    top = candidates[0]
    assert top["kind"] == "Product"
    category = _by(candidates, "Product", "category", "value")
    assert category["values"][0] == "Borse"  # the nearest Record's, counted from the index
    assert category["records"] == 100
    assert disabled.json()["candidates"] == []
    assert {"level": "embedding", "reason": "not requested"} in disabled.json()["skipped"]


MUMBAI = "".join(chr(c) for c in (0x92E, 0x941, 0x902, 0x92C, 0x908))  # Devanagari, with marks


@pytest.mark.parametrize(
    ("text", "value"),
    [
        (f"stores in {MUMBAI}", MUMBAI),
        ("shops in rock & roll", "Rock & Roll"),
        ("S/M outlets", "S/M"),
        ("stores at 50%", "50%"),
        ("Rock & Roll", "Rock & Roll"),
    ],
)
async def test_words_of_other_scripts_and_punctuated_values(
    seeded: Seeded, text: str, value: str
) -> None:
    async with keeper(seeded.app_url, SECTION) as http:
        response = await _resolve(http, seeded, {"text": text})
    city = _by(response.json()["candidates"], "Store", "city", "value")
    assert city is not None, response.json()
    assert city["values"] == [value]
    assert city["level"] == "exact"
