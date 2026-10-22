"""Embedding passes on the `supply` example with the deterministic fake model (no network)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import KindSearch, Schema
from lorebase_core.revision import Author, Origin
from lorebase_inference import InferenceError, InferenceSettings
from lorebase_inference.fake import embed_text
from lorebase_scribe.embeddings import (
    PROBE_TEXT,
    EmbeddingConfigError,
    EmbeddingModels,
    EmbeddingPass,
    Templates,
)
from lorebase_scribe.jobs import JobContext, build_handlers
from lorebase_scribe.scheduler import Scheduler
from lorebase_scribe.settings import EmbeddingJobSettings, SchedulerSettings, ScribeSettings
from lorebase_store import migrations
from lorebase_store.commit import SchemaUpsert, commit
from lorebase_store.embeddings import SchemaCache, template_fingerprints
from lorebase_store.gc import collect_garbage
from lorebase_store.jobs import EMBEDDINGS_JOB, JobRow, embeddings_payload
from lorebase_store.materialize.branch import create_branch
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, StoreSettings
from lorebase_store.webhooks import WebhookSettings

from .support import (
    DOMAIN,
    Clock,
    commit_on,
    fetch,
    load_domain,
    record_change,
    rows,
    scalar,
    supply_schema,
)

pytestmark = pytest.mark.integration

TEMPLATE = "{legal_name}, {city} ({country})"
SUPPLIERS = rows("supplier.csv")


def schema_with(template: str | None) -> Schema:
    schema = supply_schema()
    supplier = schema.kind_by_api_name("Supplier")
    assert supplier is not None
    kinds = dict(schema.kinds)
    kinds[supplier.id] = supplier.model_copy(
        update={"search": KindSearch(embedding_template=template)}
    )
    return schema.model_copy(update={"kinds": kinds})


def text_of(row: dict[str, str], template: str = TEMPLATE) -> str:
    return template.format(**row)


class CountingModel:
    """The fake model, counting the texts it embeds; optionally failing first."""

    def __init__(self, dims: int, *, name: str = "fake:test", failures: int = 0) -> None:
        self.name = name
        self.dims = dims
        self.texts: list[str] = []
        self.failures = failures

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if list(texts) != [PROBE_TEXT]:
            if self.failures:
                self.failures -= 1
                raise InferenceError("temporarily unavailable")
            self.texts.extend(texts)
        return [embed_text(t, self.dims) for t in texts]


async def no_sleep(_seconds: float) -> None:
    return None


def embedding_pass(
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    model: CountingModel,
    inference: InferenceSettings | None = None,
    **settings: Any,
) -> EmbeddingPass:
    dims = runtime.settings.embeddings.dims
    models = EmbeddingModels(inference or InferenceSettings(), dims=dims, factory=lambda: model)
    return EmbeddingPass(
        engine,
        sessions,
        models,
        EmbeddingJobSettings.model_validate(settings),
        dims=dims,
        sleep=no_sleep,
    )


async def stored(sessions: async_sessionmaker[AsyncSession], model: str) -> dict[bytes, Any]:
    found = await fetch(
        sessions,
        "SELECT r.key, e.embedding::text FROM mat_records r JOIN embeddings e "
        "ON e.domain_id = r.domain_id AND e.object_hash = r.object_hash AND e.model = :m "
        "WHERE r.domain_id = :d AND r.branch = 'main'",
        m=model,
        d=DOMAIN,
    )
    return dict(found)


async def test_a_pass_embeds_every_record_once_and_then_only_changes(
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
) -> None:
    schema = schema_with(TEMPLATE)
    await load_domain(sessions, runtime, clock, schema, kinds={"Supplier": "supplier.csv"})
    dims = runtime.settings.embeddings.dims
    model = CountingModel(dims)
    run = embedding_pass(engine, sessions, runtime, model, batch_texts=7)

    report = await run.run(DOMAIN)
    assert report.complete
    [main] = report.branches
    assert (main.rows, main.embedded, main.calls) == (len(SUPPLIERS), len(SUPPLIERS), 5)
    assert sorted(model.texts) == sorted(text_of(r) for r in SUPPLIERS)
    vectors = await stored(sessions, "fake:test")
    first = SUPPLIERS[0]
    assert len(vectors) == len(SUPPLIERS)
    stored_first = [float(v) for v in vectors[first["supplier_code"]].strip("[]").split(",")]
    assert stored_first == pytest.approx(embed_text(text_of(first), dims), abs=1e-6)

    # Nothing moved: the branch is skipped without reading rows.
    again = await run.run(DOMAIN)
    assert again.branches[0].skipped
    assert len(model.texts) == len(SUPPLIERS)

    # One Supplier changes: one row read, one text embedded.
    supplier = schema.kind_by_api_name("Supplier")
    assert supplier is not None
    await commit_on(sessions, runtime, clock, [record_change(supplier, {**first, "city": "Kyiv"})])
    changed = await run.run(DOMAIN)
    assert (changed.branches[0].rows, changed.branches[0].embedded) == (1, 1)
    assert model.texts[-1] == text_of({**first, "city": "Kyiv"})

    # A change outside the template gives a new object hash with the same text: the vector of
    # that text is reused, no model call.
    calls = len(model.texts)
    await commit_on(
        sessions, runtime, clock, [record_change(supplier, {**first, "city": "Kyiv", "tier": "2"})]
    )
    reused = await run.run(DOMAIN)
    assert (reused.branches[0].reused, reused.branches[0].embedded) == (1, 0)
    assert len(model.texts) == calls


async def test_document_prefix_is_embedded_and_a_change_embeds_everything_again(
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
) -> None:
    await load_domain(
        sessions, runtime, clock, schema_with(TEMPLATE), kinds={"Supplier": "supplier.csv"}
    )
    model = CountingModel(runtime.settings.embeddings.dims)

    def prefixed(prefix: str) -> EmbeddingPass:
        inference = InferenceSettings.model_validate({"embeddings": {"document_prefix": prefix}})
        return embedding_pass(engine, sessions, runtime, model, inference)

    report = await prefixed("passage: ").run(DOMAIN)
    assert report.branches[0].embedded == len(SUPPLIERS)
    assert sorted(model.texts) == sorted("passage: " + text_of(r) for r in SUPPLIERS)

    # Same prefix: nothing to do. Another prefix: every text again, with the new prefix.
    assert (await prefixed("passage: ").run(DOMAIN)).branches[0].skipped
    model.texts.clear()
    again = await prefixed("doc: ").run(DOMAIN)
    assert again.branches[0].embedded == len(SUPPLIERS)
    assert sorted(model.texts) == sorted("doc: " + text_of(r) for r in SUPPLIERS)


async def test_overlays_template_changes_budget_and_model_switch(
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
) -> None:
    schema = schema_with(TEMPLATE)
    await load_domain(sessions, runtime, clock, schema, kinds={"Supplier": "supplier.csv"})
    model = CountingModel(runtime.settings.embeddings.dims)
    run = embedding_pass(engine, sessions, runtime, model)
    assert (await run.run(DOMAIN)).complete

    # An overlay shares main's vectors: only the Record it changes is embedded.
    async with sessions() as session:
        await create_branch(
            session, DOMAIN, "agent/edit", created_by="usr_editor", mode="overlay", runtime=runtime
        )
    supplier = schema.kind_by_api_name("Supplier")
    assert supplier is not None
    second = SUPPLIERS[1]
    await commit_on(
        sessions,
        runtime,
        clock,
        [record_change(supplier, {**second, "legal_name": "Overlay Ltd"})],
        branch="agent/edit",
    )
    calls = len(model.texts)
    overlay = await run.run(DOMAIN)
    by_branch = {b.branch: b for b in overlay.branches}
    assert by_branch["main"].skipped
    assert (by_branch["agent/edit"].rows, by_branch["agent/edit"].embedded) == (1, 1)
    assert model.texts[calls:] == [text_of({**second, "legal_name": "Overlay Ltd"})]

    # A template change renders every text of the Kind again; a budget of 10 rows splits the
    # pass into continuations that resume where the previous one stopped.
    changed = schema_with("{legal_name} in {country}")
    new_supplier = changed.kind_by_api_name("Supplier")
    assert new_supplier is not None
    async with sessions() as session:
        await commit(
            session,
            DOMAIN,
            "main",
            [],
            schema_changes=[SchemaUpsert(new_supplier)],
            author=Author(principal="usr_modeler"),
            message="Change the embedding template",
            origin=Origin(type="operation"),
            runtime=runtime,
            clock=clock,
        )
    small = embedding_pass(engine, sessions, runtime, model, max_records_per_job=10, page_rows=4)
    calls = len(model.texts)
    report = await small.run(DOMAIN, branches=["main"])
    assert report.resume is not None, report.as_dict()
    assert report.resume[0] == "main"
    assert report.branches[0].rows == 10
    passes = 1
    while report.resume is not None:
        report = await small.run(DOMAIN, branches=["main"], resume=report.resume)
        passes += 1
    assert passes >= 3
    assert sorted(model.texts[calls:]) == sorted(
        f"{r['legal_name']} in {r['country']}" for r in SUPPLIERS
    )
    assert (await small.run(DOMAIN, branches=["main"])).branches[0].skipped

    # Another model: everything is embedded again under its name; prune drops the old ones.
    other = CountingModel(runtime.settings.embeddings.dims, name="fake:other")
    switched = embedding_pass(engine, sessions, runtime, other)
    result = await switched.run(DOMAIN, prune=True)
    assert result.complete
    # main under its new template, and the overlay (promoted to a copy before main's schema
    # change, so still on the first template) under its own: no vector serves both.
    assert len(other.texts) == 2 * len(SUPPLIERS)
    assert result.pruned > 0
    models = await fetch(sessions, "SELECT DISTINCT model FROM embeddings")
    assert [m for (m,) in models] == ["fake:other"]


async def test_failures_retries_and_checks(
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
) -> None:
    await load_domain(
        sessions, runtime, clock, schema_with(TEMPLATE), kinds={"Supplier": "supplier.csv"}
    )
    dims = runtime.settings.embeddings.dims
    flaky = CountingModel(dims, failures=1)
    assert (await embedding_pass(engine, sessions, runtime, flaky).run(DOMAIN)).complete

    wrong = CountingModel(dims + 1, name="fake:wrong")
    with pytest.raises(EmbeddingConfigError, match=r"store\.embeddings\.dims"):
        await embedding_pass(engine, sessions, runtime, wrong).run(DOMAIN)

    failing = CountingModel(dims, name="fake:down", failures=5)
    with pytest.raises(InferenceError):
        await embedding_pass(engine, sessions, runtime, failing, retry={"attempts": 2}).run(DOMAIN)


async def test_kinds_without_template_need_no_model(
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
) -> None:
    await load_domain(
        sessions, runtime, clock, schema_with(None), kinds={"Supplier": "supplier.csv"}
    )

    def broken() -> CountingModel:
        raise AssertionError("no model is needed")

    dims = runtime.settings.embeddings.dims
    run = EmbeddingPass(
        engine,
        sessions,
        EmbeddingModels(InferenceSettings(), dims=dims, factory=broken),
        EmbeddingJobSettings(),
        dims=dims,
    )
    report = await run.run(DOMAIN)
    assert report.complete
    assert await scalar(sessions, "SELECT count(*) FROM embeddings") == 0


async def test_job_continuation_and_schedule(
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
) -> None:
    await load_domain(
        sessions, runtime, clock, schema_with(TEMPLATE), kinds={"Supplier": "supplier.csv"}
    )
    dims = runtime.settings.embeddings.dims
    model = CountingModel(dims)
    async with httpx.AsyncClient() as http:
        context = JobContext(
            engine=engine,
            sessions=sessions,
            runtime=runtime,
            settings=ScribeSettings.model_validate({"embeddings": {"max_records_per_job": 20}}),
            webhooks=WebhookSettings(),
            http=http,
            worker_id="test",
            embedding_models=EmbeddingModels(InferenceSettings(), dims=dims, factory=lambda: model),
        )
        handler = build_handlers(context, [EMBEDDINGS_JOB])[EMBEDDINGS_JOB]
        now = datetime.now(UTC)
        job = JobRow(1, EMBEDDINGS_JOB, DOMAIN, embeddings_payload(), 0, 1, now, "test", now)
        result = await handler(job)
        assert result["complete"] is False
        continuation = await fetch(
            sessions, "SELECT payload FROM jobs WHERE id = :id", id=result["continuation"]
        )
        payload = continuation[0][0]
        assert payload["resume"][0] == "main"
        follow = JobRow(2, EMBEDDINGS_JOB, DOMAIN, payload, 0, 1, now, "test", now)
        assert (await handler(follow))["complete"] is True
        assert len(model.texts) == len(SUPPLIERS)

    scheduler = Scheduler(
        engine,
        sessions,
        settings=SchedulerSettings(
            webhook_interval_s=0,
            gc_schedule=None,
            embeddings_interval_s=60,
            audit_verify_interval_s=0,
        ),
        jobs=runtime.settings.jobs,
        writes=runtime.settings.writes,
    )
    try:
        first = await scheduler.tick()
        assert len(first) == 1
        assert await scheduler.tick() == []  # within the interval
    finally:
        await scheduler.release()
    queued = await fetch(
        sessions, "SELECT type, domain_id, payload FROM jobs WHERE id = :id", id=first[0]
    )
    assert [tuple(r) for r in queued] == [(EMBEDDINGS_JOB, DOMAIN, {})]


async def change_template(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    template: str,
    *,
    branch: str = "main",
) -> None:
    supplier = schema_with(template).kind_by_api_name("Supplier")
    assert supplier is not None
    async with sessions() as session:
        await commit(
            session,
            DOMAIN,
            branch,
            [],
            schema_changes=[SchemaUpsert(supplier)],
            author=Author(principal="usr_modeler"),
            message="Change the embedding template",
            origin=Origin(type="operation"),
            runtime=runtime,
            clock=clock,
        )


async def searched(
    sessions: async_sessionmaker[AsyncSession], branch: str, template: str, model: str
) -> dict[str, list[float]]:
    """The Supplier vectors a search of `branch` reads: those under the fingerprint of the
    branch's template, as Keeper looks them up."""
    supplier = schema_with(template).kind_by_api_name("Supplier")
    assert supplier is not None
    found = await fetch(
        sessions,
        "SELECT r.key, e.embedding::text FROM mat_records r JOIN embeddings e "
        "ON e.domain_id = r.domain_id AND e.object_hash = r.object_hash AND e.model = :m "
        "AND e.fingerprint = :f WHERE r.domain_id = :d AND r.branch = :b AND r.kind_id = :k",
        m=model,
        f=template_fingerprints([supplier])[supplier.id],
        d=DOMAIN,
        b=branch,
        k=supplier.id,
    )
    return {key: [float(v) for v in vector.strip("[]").split(",")] for key, vector in found}


def assert_searched(
    vectors: dict[str, list[float]], template: str, dims: int, rows: Sequence[dict[str, str]]
) -> None:
    assert sorted(vectors) == sorted(r["supplier_code"] for r in rows)
    for row in rows:
        expected = embed_text(text_of(row, template), dims)
        assert vectors[row["supplier_code"]] == pytest.approx(expected, abs=1e-6)


async def full_branch(sessions: async_sessionmaker[AsyncSession], runtime: StoreRuntime) -> None:
    async with sessions() as session:
        await create_branch(
            session, DOMAIN, "dev", created_by="usr_editor", mode="copy", runtime=runtime
        )


async def test_branches_whose_templates_differ_keep_their_own_vectors(
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
) -> None:
    await load_domain(
        sessions, runtime, clock, schema_with(TEMPLATE), kinds={"Supplier": "supplier.csv"}
    )
    await full_branch(sessions, runtime)
    other = "{legal_name} in {country}"
    await change_template(sessions, runtime, clock, other, branch="dev")
    dims = runtime.settings.embeddings.dims
    model = CountingModel(dims)
    run = embedding_pass(engine, sessions, runtime, model)

    report = await run.run(DOMAIN)
    assert report.complete
    by_branch = {b.branch: b for b in report.branches}
    assert by_branch["main"].embedded == by_branch["dev"].embedded == len(SUPPLIERS)
    assert await scalar(sessions, "SELECT count(*) FROM embeddings") == 2 * len(SUPPLIERS)
    main_vectors = await searched(sessions, "main", TEMPLATE, model.name)
    assert_searched(main_vectors, TEMPLATE, dims, SUPPLIERS)
    assert_searched(await searched(sessions, "dev", other, model.name), other, dims, SUPPLIERS)

    # Later passes overwrite neither branch's vectors.
    assert all(b.skipped for b in (await run.run(DOMAIN)).branches)
    assert await searched(sessions, "main", TEMPLATE, model.name) == main_vectors

    # dev changes its template again: the vectors of the template no branch uses any more are
    # collected, main's stay.
    third = "{legal_name}"
    await change_template(sessions, runtime, clock, third, branch="dev")
    assert (await run.run(DOMAIN)).complete
    assert_searched(await searched(sessions, "dev", third, model.name), third, dims, SUPPLIERS)
    gc = await collect_garbage(engine, DOMAIN, runtime=runtime)
    assert gc.embeddings == len(SUPPLIERS)
    assert await searched(sessions, "dev", other, model.name) == {}
    assert await searched(sessions, "main", TEMPLATE, model.name) == main_vectors
    assert_searched(await searched(sessions, "dev", third, model.name), third, dims, SUPPLIERS)


async def test_templates_are_resolved_once_per_schema(
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
) -> None:
    await load_domain(
        sessions, runtime, clock, schema_with(TEMPLATE), kinds={"Supplier": "supplier.csv"}
    )
    await full_branch(sessions, runtime)
    dims = runtime.settings.embeddings.dims
    model = CountingModel(dims)
    settings = EmbeddingJobSettings()
    cache: SchemaCache[Templates] = SchemaCache(settings.template_cache_size)
    run = EmbeddingPass(
        engine,
        sessions,
        EmbeddingModels(InferenceSettings(), dims=dims, factory=lambda: model),
        settings,
        dims=dims,
        sleep=no_sleep,
        templates=cache,
    )

    assert (await run.run(DOMAIN)).complete
    assert (cache.misses, cache.hits) == (1, 1)  # two branches, one schema
    assert (await run.run(DOMAIN)).complete
    assert (cache.misses, cache.hits) == (1, 3)

    # A schema change on one branch: only its new schema is read.
    await change_template(sessions, runtime, clock, "{legal_name}", branch="dev")
    assert (await run.run(DOMAIN)).complete
    assert (cache.misses, cache.hits) == (2, 4)

    # The cache keeps the schemas used last.
    small: SchemaCache[str] = SchemaCache(1)
    small.put(b"a", "first")
    small.put(b"b", "second")
    assert (small.get(b"a"), small.get(b"b")) == (None, "second")


async def test_migration_keys_existing_vectors_by_template(
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    database_url: str,
) -> None:
    schema = schema_with(TEMPLATE)
    await load_domain(sessions, runtime, clock, schema, kinds={"Supplier": "supplier.csv"})
    await full_branch(sessions, runtime)
    other = "{legal_name} in {country}"
    await change_template(sessions, runtime, clock, other, branch="dev")
    supplier = schema.kind_by_api_name("Supplier")
    assert supplier is not None
    first = SUPPLIERS[0]
    moved = {**first, "legal_name": "Moved Ltd"}
    await commit_on(sessions, runtime, clock, [record_change(supplier, moved)], branch="dev")
    dims = runtime.settings.embeddings.dims
    model = CountingModel(dims)
    assert (await embedding_pass(engine, sessions, runtime, model).run(DOMAIN)).complete

    # Back to one vector per object hash, then forward again.
    def migrate(step: Callable[..., None], revision: str) -> None:
        step(revision, database=DatabaseSettings(), store=StoreSettings(), url=database_url)

    await asyncio.to_thread(migrate, migrations.downgrade, "0023")
    assert await scalar(sessions, "SELECT count(*) FROM embeddings") == len(SUPPLIERS) + 1
    await asyncio.to_thread(migrate, migrations.upgrade, "head")

    by_template: dict[str, str] = {}
    for template in (TEMPLATE, other):
        kind = schema_with(template).kind_by_api_name("Supplier")
        assert kind is not None
        by_template[template] = template_fingerprints([kind])[kind.id]
    keyed = dict(
        await fetch(
            sessions,
            "SELECT r.key, e.fingerprint FROM mat_records r JOIN embeddings e "
            "ON e.domain_id = r.domain_id AND e.object_hash = r.object_hash "
            "WHERE r.branch = :b",
            b="dev",
        )
    )
    # Only dev holds its moved Supplier, so that vector's template is known; the rows both
    # branches hold under different templates are not.
    code = first["supplier_code"]
    assert keyed[code] == by_template[other]
    assert {f for k, f in keyed.items() if k != code} == {""}
    main_first = await fetch(
        sessions,
        "SELECT e.fingerprint FROM mat_records r JOIN embeddings e "
        "ON e.domain_id = r.domain_id AND e.object_hash = r.object_hash "
        "WHERE r.branch = 'main' AND r.key = :k",
        k=code,
    )
    assert [f for (f,) in main_first] == [by_template[TEMPLATE]]
    cursors = await fetch(sessions, "SELECT fingerprints FROM embedding_cursors")
    assert cursors
    assert all(f == {} for (f,) in cursors)

    # The next pass renders every text again: the texts whose vector survived are copied,
    # only the others are embedded, and each branch searches its own template's vectors.
    model.texts.clear()
    assert (await embedding_pass(engine, sessions, runtime, model).run(DOMAIN)).complete
    assert len(model.texts) == len(SUPPLIERS) - 1
    main_vectors = await searched(sessions, "main", TEMPLATE, model.name)
    assert_searched(main_vectors, TEMPLATE, dims, SUPPLIERS)
    dev_vectors = await searched(sessions, "dev", other, model.name)
    assert_searched(dev_vectors, other, dims, [moved, *SUPPLIERS[1:]])
