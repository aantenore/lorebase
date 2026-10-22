"""Branches forked before the default branch tagged an Attribute (design 10.2): the batched
rewrite of their stale titles and search vectors, its automatic queueing, and the schema
change that gives them the default branch's tags."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.expr.sensitivity import rebased_tags, schema_tags
from lorebase_core.model import Schema, Sensitivity
from lorebase_store.commit import SchemaUpsert, commit, create_domain
from lorebase_store.commit.schema import load_compiled
from lorebase_store.jobs import SENSITIVITY_REFRESH_JOB, JobRow
from lorebase_store.materialize import (
    BranchMode,
    DivergenceReason,
    create_branch,
    run_sensitivity_refresh_job,
    stale_kinds,
    verify,
)
from lorebase_store.repositories import MaterializedRepository, RefRepository, scope_to_domain
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import CommitSettings, MaterializeSettings, StoreSettings

from .support import (
    AUTHOR,
    COUNTRY,
    DOMAIN,
    LEGAL_NAME,
    ORIGIN,
    SUPPLIER,
    Clock,
    domain,
    part,
    schema_changes,
    supplier,
    supplier_kind,
)

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
STALE = "stale"
OVERLAY = "stale-overlay"
PII = (Sensitivity.PII,)
SUPPLIERS = {"S-1": "Acme Bearings", "S-2": "Bolt Works", "S-3": "Cog Foundry"}


def runtime_with(**materialize: Any) -> StoreRuntime:
    return StoreRuntime.from_settings(StoreSettings(materialize=MaterializeSettings(**materialize)))


ONE_A_BATCH = runtime_with(job_batch_rows=1)


def job(branch: str, **payload: Any) -> JobRow:
    now = datetime.now(UTC)
    return JobRow(
        id=0,
        type=SENSITIVITY_REFRESH_JOB,
        domain_id=DOMAIN,
        payload={"branch": branch, **payload},
        priority=0,
        attempts=1,
        run_after=now,
        locked_by="test",
        created_at=now,
    )


async def run(
    sessions: Sessions,
    runtime: StoreRuntime,
    clock: Clock,
    changes: list[Any],
    branch: str = "main",
    **options: Any,
) -> Any:
    async with sessions() as session:
        return await commit(
            session,
            DOMAIN,
            branch,
            changes,
            author=AUTHOR,
            message="test",
            origin=ORIGIN,
            runtime=runtime,
            clock=clock,
            **options,
        )


def tagged_supplier() -> Any:
    kind = supplier_kind()
    name = kind.attributes[LEGAL_NAME].model_copy(update={"sensitivity": PII})
    return kind.model_copy(update={"attributes": {**kind.attributes, LEGAL_NAME: name}})


@pytest.fixture
async def sessions(engine: AsyncEngine, runtime: StoreRuntime) -> Sessions:
    """`main` with three Suppliers; `stale` and its overlay forked before `main` tags the
    Supplier's legal name (its title and a searchable Attribute)."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    clock = Clock()
    async with factory() as session:
        await create_domain(session, domain(), author=AUTHOR, runtime=runtime, clock=clock)
    rows = [supplier(code, name) for code, name in SUPPLIERS.items()]
    await run(factory, runtime, clock, rows, schema_changes=schema_changes())
    async with factory() as session:
        await create_branch(
            session, DOMAIN, STALE, created_by="usr_test", mode=BranchMode.COPY, runtime=runtime
        )
    async with factory() as session:
        await create_branch(
            session,
            DOMAIN,
            OVERLAY,
            created_by="usr_test",
            source=STALE,
            mode=BranchMode.OVERLAY,
            runtime=runtime,
        )
    # A later commit on `stale`: rewriting its older rows copies them into the overlay first.
    await run(factory, runtime, clock, [part("P-1", "Bearing")], branch=STALE)
    await run(factory, runtime, clock, [], schema_changes=[SchemaUpsert(tagged_supplier())])
    return factory


async def queued(sessions: Sessions) -> list[dict[str, Any]]:
    async with sessions() as session, session.begin():
        found = await session.execute(
            text("SELECT payload FROM jobs WHERE type = :t AND status = 'queued' ORDER BY id"),
            {"t": SENSITIVITY_REFRESH_JOB},
        )
        return [dict(payload) for (payload,) in found.all()]


async def stale_titles(sessions: Sessions, runtime: StoreRuntime, branch: str) -> list[str]:
    async with sessions() as session, session.begin():
        report = await verify(session, DOMAIN, branch, runtime=runtime, deep=True)
    assert all(d.reason is DivergenceReason.SENSITIVITY_STALE for d in report.divergences)
    return sorted(str(d.key) for d in report.divergences)


async def indexed(sessions: Sessions, word: str) -> list[str]:
    """Keys of the Suppliers of `stale` whose search vector holds `word`."""
    async with sessions() as session, session.begin():
        found = await session.execute(
            text(
                "SELECT key FROM mat_records WHERE domain_id = :d AND branch = :b "
                "AND kind_id = :k AND search @@ plainto_tsquery('simple', :w) ORDER BY key"
            ),
            {"d": DOMAIN, "b": STALE, "k": SUPPLIER, "w": word},
        )
        return [key for (key,) in found.all()]


async def schema_of(sessions: Sessions, branch: str) -> Schema:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        stored = await MaterializedRepository(
            session, DOMAIN, branch, writes=StoreSettings().writes
        ).schema()
    assert stored is not None
    return load_compiled(stored.compiled)


async def test_a_tag_on_the_default_branch_queues_the_rewrite_of_every_other_branch(
    sessions: Sessions,
) -> None:
    assert await queued(sessions) == [{"branch": STALE}, {"branch": OVERLAY}]


async def test_no_rewrite_is_queued_when_the_setting_is_off(
    engine: AsyncEngine, runtime: StoreRuntime
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    clock = Clock()
    off = runtime_with(refresh_branches_on_tag=False)
    async with factory() as session:
        await create_domain(session, domain(), author=AUTHOR, runtime=runtime, clock=clock)
    await run(factory, runtime, clock, [], schema_changes=schema_changes())
    async with factory() as session:
        await create_branch(session, DOMAIN, STALE, created_by="usr_test", runtime=runtime)
    await run(factory, off, clock, [], schema_changes=[SchemaUpsert(tagged_supplier())])
    assert await queued(factory) == []


async def test_the_job_rewrites_stale_titles_and_vectors_in_batches(
    sessions: Sessions, engine: AsyncEngine
) -> None:
    assert await stale_titles(sessions, ONE_A_BATCH, STALE) == sorted(SUPPLIERS)
    assert await indexed(sessions, "acme") == ["S-1"]
    own = await schema_of(sessions, STALE)
    assert own.kinds[SUPPLIER].attributes[LEGAL_NAME].sensitivity == ()

    result = await run_sensitivity_refresh_job(engine, job(STALE), runtime=ONE_A_BATCH)
    assert (result["status"], result["kinds"]) == ("done", [SUPPLIER])
    assert (result["batches"], result["titles"]) == (len(SUPPLIERS), len(SUPPLIERS))
    assert await stale_titles(sessions, ONE_A_BATCH, STALE) == []
    assert await indexed(sessions, "acme") == []
    # The schema of the branch is left as it was.
    assert (await schema_of(sessions, STALE)).kinds[SUPPLIER] == own.kinds[SUPPLIER]

    # The overlay got the old rows copied in before they changed: it is queued again.
    assert result["overlays"]
    assert await stale_titles(sessions, ONE_A_BATCH, OVERLAY) == sorted(SUPPLIERS)
    on_overlay = await run_sensitivity_refresh_job(engine, job(OVERLAY), runtime=ONE_A_BATCH)
    assert on_overlay["titles"] == len(SUPPLIERS)
    assert await stale_titles(sessions, ONE_A_BATCH, OVERLAY) == []

    again = await run_sensitivity_refresh_job(engine, job(STALE), runtime=ONE_A_BATCH)
    assert (again["titles"], again["vectors"]) == (0, 0)
    assert "overlays" not in again


async def test_a_busy_branch_defers_the_job_with_where_it_stopped(
    sessions: Sessions, engine: AsyncEngine
) -> None:
    impatient = StoreRuntime.from_settings(
        StoreSettings(
            commit=CommitSettings(lock_timeout_ms=50),
            materialize=MaterializeSettings(job_batch_rows=1, job_retry_seconds=0),
        )
    )
    resume = [SUPPLIER, "S-1"]
    async with sessions() as holder, holder.begin():
        await scope_to_domain(holder, DOMAIN)
        await RefRepository(holder, DOMAIN).lock(STALE, timeout_ms=0)
        result = await run_sensitivity_refresh_job(
            engine, job(STALE, resume=resume), runtime=impatient
        )
    assert (result["status"], result["resume"]) == ("deferred", resume)
    assert {"branch": STALE, "resume": resume} in await queued(sessions)
    resumed = await run_sensitivity_refresh_job(
        engine, job(STALE, resume=resume), runtime=ONE_A_BATCH
    )
    # S-1 was left to the batches before the cursor.
    assert resumed["titles"] == len(SUPPLIERS) - 1
    assert await stale_titles(sessions, ONE_A_BATCH, STALE) == ["S-1"]


async def test_the_tag_rebase_commit_leaves_nothing_stale(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    own = await schema_of(sessions, STALE)
    main = await schema_of(sessions, "main")
    target = rebased_tags(own, schema_tags(main))
    assert stale_kinds(own, target) == [SUPPLIER]
    await run(
        sessions,
        runtime,
        Clock(datetime(2027, 1, 1, tzinfo=UTC)),
        [],
        branch=STALE,
        schema_changes=[SchemaUpsert(target.kinds[SUPPLIER])],
    )
    rebased = await schema_of(sessions, STALE)
    assert rebased.kinds[SUPPLIER].attributes[LEGAL_NAME].sensitivity == PII
    assert await stale_titles(sessions, runtime, STALE) == []
    assert await indexed(sessions, "acme") == []


async def test_vectors_of_a_tagged_searchable_attribute_are_rewritten(
    engine: AsyncEngine, runtime: StoreRuntime
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    clock = Clock()
    async with factory() as session:
        await create_domain(session, domain(), author=AUTHOR, runtime=runtime, clock=clock)
    rows = [supplier(code, name, country="IT") for code, name in SUPPLIERS.items()]
    await run(factory, runtime, clock, rows, schema_changes=schema_changes())
    async with factory() as session:
        await create_branch(session, DOMAIN, STALE, created_by="usr_test", runtime=runtime)
    kind = supplier_kind()
    country = kind.attributes[COUNTRY].model_copy(update={"sensitivity": PII})
    kind = kind.model_copy(update={"attributes": {**kind.attributes, COUNTRY: country}})
    await run(factory, runtime, clock, [], schema_changes=[SchemaUpsert(kind)])
    assert await indexed(factory, "it") == sorted(SUPPLIERS)

    result = await run_sensitivity_refresh_job(engine, job(STALE), runtime=ONE_A_BATCH)
    assert (result["titles"], result["vectors"]) == (0, len(SUPPLIERS))
    assert await indexed(factory, "it") == []
    assert await indexed(factory, "acme") == ["S-1"]  # the title is not sensitive
    again = await run_sensitivity_refresh_job(engine, job(STALE), runtime=ONE_A_BATCH)
    assert (again["titles"], again["vectors"]) == (0, 0)
