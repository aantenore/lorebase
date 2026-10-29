"""Read presets (design 8.8) against the `supply` example in Postgres: Traits as search targets
and context seeds, lenses, and vector search that keeps its filters before its limit."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Lens, Schema
from lorebase_core.model.ids import IdPrefix, new_id
from lorebase_store.embeddings import iterative_scans, template_fingerprints
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import StoreSettings

from .read_fixtures import Seeded, keeper, owner_session, seeded_database
from .supply import DOMAIN, Clock, load_supply, rows, supply_schema

pytestmark = pytest.mark.integration

BASE = f"/api/v1/domains/{DOMAIN}"
DIMS = StoreSettings().embeddings.dims
OBSOLETE = {r["part_number"] for r in rows("part.csv") if r["lifecycle"] == "obsolete"}


def unit_vector(index: int) -> list[float]:
    vector = [0.0] * DIMS
    vector[index % DIMS] = 1.0
    vector[(index + 1) % DIMS] = 0.5
    return vector


def with_presets() -> Schema:
    """The example schema with Parts embedded by name and three lenses."""
    schema = supply_schema()

    def ids(section: str, *names: str) -> tuple[str, ...]:
        found = {e.api_name: e.id for e in getattr(schema, section).values()}
        return tuple(found[name] for name in names)

    part = schema.kind_by_api_name("Part")
    assert part is not None
    search = part.search.model_copy(update={"embedding_template": "{name}"})
    lenses = [
        Lens(
            id=new_id(IdPrefix.LENS),
            api_name="supplier_view",
            description="A supplier with the parts it supplies.",
            kinds=ids("kinds", "Supplier", "Part"),
            relations=ids("relations", "supplied_by"),
            depth=1,
            include_lineage=False,
        ),
        Lens(
            id=new_id(IdPrefix.LENS),
            api_name="located",
            description="Whatever has a country.",
            traits=ids("traits", "Located"),
        ),
        Lens(id=new_id(IdPrefix.LENS), api_name="anything", description="No limits."),
    ]
    return schema.model_copy(
        update={
            "kinds": {**schema.kinds, part.id: part.model_copy(update={"search": search})},
            "lenses": {lens.id: lens for lens in lenses},
        }
    )


async def seed(
    session: AsyncSession, runtime: StoreRuntime, clock: Clock, owner_url: str, app_url: str
) -> Seeded:
    schema, _ = await load_supply(session, runtime, clock, with_presets())
    part = schema.kind_by_api_name("Part")
    assert part is not None
    fingerprint = template_fingerprints([part])[part.id]
    async with session.begin():
        found = await session.execute(
            text(
                "SELECT key, object_hash FROM mat_records "
                "WHERE domain_id = :d AND branch = 'main' AND kind_id = :k"
            ),
            {"d": DOMAIN, "k": part.id},
        )
        for index, (_, digest) in enumerate(sorted(found)):
            await session.execute(
                text(
                    "INSERT INTO embeddings "
                    "(domain_id, object_hash, model, fingerprint, dims, embedding) "
                    "VALUES (:d, :h, :m, :f, :n, CAST(:v AS vector))"
                ),
                {
                    "d": DOMAIN,
                    "h": digest,
                    "m": "BAAI/bge-small-en-v1.5",
                    "f": fingerprint,
                    "n": DIMS,
                    "v": str(unit_vector(index)),
                },
            )
    async with owner_session(owner_url) as other:
        facts = {"iterative": await iterative_scans(other)}
    return Seeded(owner_url=owner_url, app_url=app_url, schema=schema, facts=facts)


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


async def search(http: httpx.AsyncClient, body: dict[str, Any], status: int = 200) -> Any:
    response = await http.post(f"{BASE}/search", json=body)
    assert response.status_code == status, response.text
    return response.json()


# Traits and lenses ------------------------------------------------------------------------------


async def test_traits_and_lenses_choose_what_search_covers(seeded: Seeded) -> None:
    async with keeper(seeded.app_url) as http:
        located = await search(http, {"q": "Kavsen", "traits": ["Located"]})
        assert {hit["kind"] for hit in located["items"]} <= {"Supplier", "Plant"}
        assert "S-1001" in {hit["key"] for hit in located["items"]}
        by_lens = await search(http, {"q": "Kavsen", "lens": "located"})
        assert [h["key"] for h in by_lens["items"]] == [h["key"] for h in located["items"]]

        parts = await search(http, {"q": "bearing", "lens": "supplier_view"})
        assert parts["items"]
        assert {hit["kind"] for hit in parts["items"]} <= {"Supplier", "Part"}
        # What the request names wins over the lens.
        plants = await search(http, {"q": "bearing", "lens": "supplier_view", "kinds": ["Plant"]})
        assert {hit["kind"] for hit in plants["items"]} <= {"Plant"}
        # Kinds and Traits together are the union.
        union = await search(http, {"q": "Kavsen", "kinds": ["Part"], "traits": ["Located"]})
        assert "S-1001" in {hit["key"] for hit in union["items"]}

        unknown = await search(http, {"q": "x", "traits": ["Nowhere"]}, 422)
        assert unknown["errors"][0]["code"] == "unknown_trait"
        no_lens = await search(http, {"q": "x", "lens": "nothing"}, 422)
        assert no_lens["errors"][0] == {
            "pointer": "/body/lens",
            "message": "unknown lens 'nothing'",
            "code": "lens_unknown",
        }


async def test_a_lens_shapes_the_context_pack_and_refs_are_always_kept(seeded: Seeded) -> None:
    async with keeper(seeded.app_url) as http:
        asked = {"refs": ["Supplier/S-1001", "Plant/PL-03"], "lens": "supplier_view"}
        response = await http.post(f"{BASE}/context", json=asked)
        assert response.status_code == 200, response.text
        pack = response.json()
        assert {s["ref"] for s in pack["seeds"]} == {"Supplier/S-1001", "Plant/PL-03"}
        # Neighbors are Parts only, over supplied_by only; lineage is off.
        assert pack["neighbors"]
        assert {n["ref"].partition("/")[0] for n in pack["neighbors"]} == {"Part"}
        assert set(pack["links"]) == {"supplied_by"}
        assert "lineage" not in pack

        plain = (await http.post(f"{BASE}/context", json={"refs": ["Supplier/S-1001"]})).json()
        assert {n["ref"].partition("/")[0] for n in plain["neighbors"]} > {"Part"}

        by_trait = await http.post(
            f"{BASE}/context", json={"search": "Kavsen", "traits": ["Located"], "depth": 0}
        )
        assert by_trait.status_code == 200, by_trait.text
        assert {s["ref"].partition("/")[0] for s in by_trait.json()["seeds"]} <= {
            "Supplier",
            "Plant",
        }

        missing = await http.post(
            f"{BASE}/context", json={"refs": ["Supplier/S-1001"], "lens": "x"}
        )
        assert missing.status_code == 422
        assert missing.json()["errors"][0]["code"] == "lens_unknown"


async def test_the_schema_lists_the_lenses(seeded: Seeded) -> None:
    async with keeper(seeded.app_url) as http:
        body = (await http.get(f"{BASE}/schema")).json()
        lenses = {lens["api_name"]: lens for lens in body["schema"]["lenses"].values()}
        assert set(lenses) == {"supplier_view", "located", "anything"}
        element = await http.get(f"{BASE}/schema/lenses/supplier_view")
        assert element.status_code == 200
        assert element.json()["depth"] == 1
        ready = (await http.get("/readyz")).json()
        assert ready["checks"]["vector_search"]["status"] == "ok"
        assert "pgvector" in ready["checks"]["vector_search"]["detail"]


# Filtered vector search -------------------------------------------------------------------------

NEAREST = {"query_vector": unit_vector(0), "kinds": ["Part"]}
OBSOLETE_ONLY = {"where": {"attr": "lifecycle", "op": "eq", "value": "obsolete"}}
FEW = {"tuning": {"candidates": 3}}


async def test_filters_stay_before_the_limit_and_nothing_leaks(seeded: Seeded) -> None:
    async with keeper(seeded.app_url) as http:
        found = await search(http, {**NEAREST, **OBSOLETE_ONLY, "limit": 50})
        assert {hit["key"] for hit in found["items"]} == OBSOLETE
        assert found["semantic_truncated"] is False
        scores = [hit["score"] for hit in found["items"]]
        assert scores == sorted(scores, reverse=True)

        hybrid = await search(http, {**NEAREST, **OBSOLETE_ONLY, "q": "bearing", "limit": 50})
        assert {hit["key"] for hit in hybrid["items"]} <= OBSOLETE
        assert hybrid["semantic_truncated"] is False


async def test_iterative_scans_fill_the_list_the_filters_would_shorten(seeded: Seeded) -> None:
    async with keeper(seeded.app_url) as http:
        found = await search(http, {**NEAREST, **OBSOLETE_ONLY, **FEW})
        if seeded.facts["iterative"]:
            assert len(found["items"]) == 3
            assert {hit["key"] for hit in found["items"]} <= OBSOLETE
            assert found["semantic_truncated"] is False
    async with keeper(seeded.app_url, search={"semantic": {"iterative_scan": "off"}}) as http:
        wide = await search(http, {**NEAREST, **OBSOLETE_ONLY, "limit": 50})
        assert {hit["key"] for hit in wide["items"]} == OBSOLETE


async def test_without_iterative_scans_the_index_is_over_fetched_and_the_cut_is_reported(
    seeded: Seeded,
) -> None:
    semantic = {"iterative_scan": "off", "overfetch_factor": 1}
    async with keeper(seeded.app_url, search={"semantic": semantic}) as http:
        # The 3 nearest Parts are all active: no obsolete Part among them, yet 4 match.
        cut = await search(http, {**NEAREST, **OBSOLETE_ONLY, **FEW})
        assert cut["items"] == []
        assert cut["semantic_truncated"] is True
        # Nothing matching the filters is lost for lack of room: the list fills up.
        semantic_all = await search(http, {**NEAREST, "tuning": {"candidates": 3}})
        assert len(semantic_all["items"]) == 3
        assert semantic_all["semantic_truncated"] is False
        # A list short for lack of matches is not flagged.
        none = await search(
            http,
            {
                "query_vector": unit_vector(0),
                "kinds": ["Part"],
                "where": {"attr": "part_number", "op": "eq", "value": "P-99999"},
            },
            200,
        )
        assert none["items"] == []
        assert none["semantic_truncated"] is False
    wide = {"iterative_scan": "off", "overfetch_factor": 40}
    async with keeper(seeded.app_url, search={"semantic": wide}) as http:
        found = await search(http, {**NEAREST, **OBSOLETE_ONLY, **FEW})
        assert {hit["key"] for hit in found["items"]} <= OBSOLETE
        assert len(found["items"]) == 3
