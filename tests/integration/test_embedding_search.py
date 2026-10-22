"""End to end: a schema with an embedding template, Records committed through Keeper, an
embedding pass of Scribe with the deterministic fake model, then hybrid search through Keeper
with a query vector computed on the caller's side. No network beyond Postgres."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from keeper_harness import IN_PROCESS_URL, PEPPER, KeeperDatabase
from sqlalchemy.ext.asyncio import async_sessionmaker

from lorebase_client import AsyncLorebaseClient
from lorebase_client import models as m
from lorebase_inference import InferenceSettings, create_embedding_model
from lorebase_scribe.embeddings import EmbeddingModels, EmbeddingPass
from lorebase_scribe.settings import EmbeddingJobSettings
from lorebase_store.db import create_engine
from lorebase_store.settings import DatabaseSettings, StoreSettings

pytestmark = pytest.mark.integration

DOMAIN = "notes"
BRANCH = "work"
DIMS = StoreSettings().embeddings.dims
INFERENCE = InferenceSettings.model_validate({"embeddings": {"provider": "fake", "dims": DIMS}})
MODEL = create_embedding_model(INFERENCE).name

SCHEMA = """
format: lorebase.schema/v1
domain: {id: notes}
kinds:
  Note:
    description: A short note.
    primary_key: [code]
    title_attribute: title
    attributes:
      code: {description: Note code., type: string, required: true}
      title: {description: Title., type: string, required: true, searchable: true}
      body: {description: Text of the note., type: text, searchable: true}
    search:
      embedding_template: "{title}. {body}"
"""

NOTES = [
    {"code": "N-1", "title": "Late deliveries", "body": "Shipments from the north plant slip."},
    {"code": "N-2", "title": "Price increase", "body": "Steel suppliers raise prices in Q3."},
    {"code": "N-3", "title": "Audit plan", "body": "Quality audit of three suppliers."},
]


@pytest.fixture
async def client(keeper_database: KeeperDatabase) -> AsyncIterator[AsyncLorebaseClient]:
    from lorebase_keeper.app import create_app
    from lorebase_platform.settings import Settings

    settings = Settings(
        keeper={
            "auth": {
                "mode": "api_key",
                "api_keys": {"pepper": PEPPER},
                "installation_admins": [keeper_database.admin_id],
                "authz": {"grants_cache_ttl_s": 0},
            }
        },
        database={"dsn": keeper_database.app_url},
        search={"semantic": {"model": MODEL}},
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        AsyncLorebaseClient(
            IN_PROCESS_URL, api_key=keeper_database.admin_key, transport=transport
        ) as admin,
    ):
        yield admin


async def embed_now(database: KeeperDatabase) -> None:
    """One Scribe embedding pass over the Domain, as the application role."""
    engine = create_engine(DatabaseSettings(), url=database.app_url, statement_timeout_ms=0)
    try:
        models = EmbeddingModels(INFERENCE, dims=DIMS)
        run = EmbeddingPass(
            engine,
            async_sessionmaker(engine, expire_on_commit=False),
            models,
            EmbeddingJobSettings(),
            dims=DIMS,
        )
        report = await run.run(DOMAIN)
        assert report.complete
        assert sum(b.embedded for b in report.branches) == len(NOTES)
    finally:
        await engine.dispose()


async def query_vector(text: str) -> list[float]:
    """What a client computes on its side (`lorebase-inference embed`)."""
    [vector] = await create_embedding_model(INFERENCE).embed([text])
    return vector


async def test_commit_embed_and_hybrid_search(
    client: AsyncLorebaseClient, keeper_database: KeeperDatabase
) -> None:
    await client.domains.create(DOMAIN, display_name="Notes")
    await client.branches.create(DOMAIN, BRANCH)
    applied = await client.schema.apply(DOMAIN, BRANCH, yaml=SCHEMA, message="Notes schema")
    assert applied.revision is not None
    imported = await client.operations.import_records(DOMAIN, "Note", NOTES, branch=BRANCH)
    assert imported.status == "committed", imported.violations

    advertised = (await client.schema.get(DOMAIN, branch=BRANCH)).search
    assert advertised.modes == ["lexical", "semantic", "hybrid"]
    assert advertised.embedding == m.EmbeddingModel(model=MODEL, dims=DIMS)

    vector = await query_vector("Price increase. Steel suppliers raise prices in Q3.")
    before = await client.records.search(
        DOMAIN, m.SearchRequest(branch=BRANCH, query_vector=vector, mode="semantic")
    )
    assert before.items == [], "no vector before the embedding pass"

    await embed_now(keeper_database)

    semantic = await client.records.search(
        DOMAIN, m.SearchRequest(branch=BRANCH, query_vector=vector, mode="semantic")
    )
    assert semantic.items[0].key == "N-2"
    assert semantic.items[0].score == pytest.approx(1.0, abs=1e-6)

    hybrid = await client.records.search(
        DOMAIN, m.SearchRequest(branch=BRANCH, q="suppliers", query_vector=vector)
    )
    assert hybrid.mode == "hybrid"
    assert hybrid.items[0].key == "N-2"
    assert {hit.key for hit in hybrid.items} >= {"N-2", "N-3"}
