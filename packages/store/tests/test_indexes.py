"""Per-Attribute partial indexes on `mat_records` (lorebase_store.indexes)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import Kind, Relation, RelationBacking, Schema
from lorebase_core.types import AttributeType, TypeSpec
from lorebase_store.commit import SchemaUpsert, commit, create_domain
from lorebase_store.indexes import (
    INDEX_JOB_TYPE,
    INDEX_PREFIX,
    BranchIndexes,
    IndexMethod,
    IndexReason,
    create_index_sql,
    existing_indexes,
    pending_index_plan,
    plan_indexes,
    predicate_sql,
    run_index_job,
    run_index_jobs,
    sql_literal,
)
from lorebase_store.jobs import INDEX_JOB, JobRow, run_jobs
from lorebase_store.materialize import create_branch, promote, verify
from lorebase_store.repositories import scope_to_domain
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, IndexSettings, StoreSettings

from .support import (
    AUTHOR,
    COUNTRY,
    DOMAIN,
    LEGAL_NAME,
    ORIGIN,
    PART,
    RATING,
    SUPPLIER,
    VAT,
    Clock,
    attribute,
    domain,
    ident,
    part_kind,
    schema_changes,
    supplied_by,
    supplier,
    supplier_kind,
)

T = TypeSpec.of
NOTES_TEXT = ident("att", 190)
TAGS = ident("att", 191)
MAIN_SUPPLIER = ident("att", 290)
SOURCED_FROM = ident("rel", 9)


def with_attributes(kind: Kind, **changes: dict[str, Any]) -> Kind:
    """Copy of `kind` with Attribute fields changed, by Attribute id."""
    attributes = dict(kind.attributes)
    for attribute_id, update in changes.items():
        attributes[attribute_id] = attributes[attribute_id].model_copy(update=update)
    return kind.model_copy(update={"attributes": attributes})


def schema_of(*kinds: Kind, relations: tuple[Relation, ...] = ()) -> Schema:
    return Schema(
        domain=domain(),
        kinds={k.id: k for k in kinds},
        relations={r.id: r for r in relations},
    )


# Planning ---------------------------------------------------------------------------------------


def test_plan_covers_indexed_unique_and_foreign_key_attributes() -> None:
    supplier_with = with_attributes(
        supplier_kind(rating=T("integer")),
        **{RATING: {"indexed": True}, COUNTRY: {"indexed": True}},
    )
    extra = [
        attribute(NOTES_TEXT, "notes_text", T("text"), indexed=True),
        attribute(TAGS, "tags", TypeSpec.array(T("string")), indexed=True),
        attribute(MAIN_SUPPLIER, "main_supplier", TypeSpec.ref(SUPPLIER)),
    ]
    part = part_kind()
    part = part.model_copy(update={"attributes": {**part.attributes, **{a.id: a for a in extra}}})
    sourced = Relation(
        id=SOURCED_FROM,
        api_name="sourced_from",
        inverse_api_name="sources",
        from_kind=PART,
        to_kind=SUPPLIER,
        backing=RelationBacking.FOREIGN_KEY,
        foreign_key_attribute=MAIN_SUPPLIER,
    )
    plan = plan_indexes(schema_of(supplier_with, part, relations=(supplied_by(), sourced)))
    specs = plan.by_attribute()

    rating = specs[(SUPPLIER, RATING)]
    assert (rating.method, rating.unique, rating.cast) == (IndexMethod.BTREE, False, "bigint")
    assert rating.columns == f"((\"values\" ->> '{RATING}')::bigint), key"
    assert rating.ordered

    vat = specs[(SUPPLIER, VAT)]
    assert vat.unique
    assert vat.reasons == (IndexReason.UNIQUE,)
    assert vat.columns == f"(\"values\" ->> '{VAT}')"

    assert specs[(SUPPLIER, COUNTRY)].cast is None
    notes = specs[(PART, NOTES_TEXT)]
    assert notes.method is IndexMethod.HASH
    assert not notes.ordered
    assert specs[(PART, MAIN_SUPPLIER)].reasons == (IndexReason.FOREIGN_KEY,)
    assert (SUPPLIER, LEGAL_NAME) not in specs
    assert [(s.attribute_id, s.attribute_type) for s in plan.skipped] == [
        (TAGS, AttributeType.ARRAY)
    ]


def test_unique_text_is_indexed_by_digest() -> None:
    kind = part_kind()
    notes = attribute(NOTES_TEXT, "notes_text", T("text"), unique=True)
    kind = kind.model_copy(update={"attributes": {**kind.attributes, NOTES_TEXT: notes}})
    spec = plan_indexes(schema_of(kind)).by_attribute()[(PART, NOTES_TEXT)]
    assert (spec.method, spec.unique) == (IndexMethod.BTREE, True)
    assert spec.expression.startswith("md5(")


def test_plan_is_deterministic_and_names_are_per_branch() -> None:
    schema = schema_of(supplier_kind(), part_kind())
    assert plan_indexes(schema) == plan_indexes(schema)
    (spec,) = plan_indexes(schema).indexes
    main = spec.name(DOMAIN, "main")
    assert main.startswith(INDEX_PREFIX)
    assert len(main) <= 63
    assert main == spec.name(DOMAIN, "main")
    assert main != spec.name(DOMAIN, "feature")
    assert main != spec.name("other", "main")


def test_overlay_plan_keeps_expressions_and_drops_uniqueness() -> None:
    rated = with_attributes(supplier_kind(rating=T("integer")), **{RATING: {"indexed": True}})
    notes = attribute(NOTES_TEXT, "notes_text", T("text"), unique=True)
    part = part_kind()
    part = part.model_copy(update={"attributes": {**part.attributes, NOTES_TEXT: notes}})
    schema = schema_of(rated, part)
    full = plan_indexes(schema).by_attribute()
    overlay = plan_indexes(schema, overlay=True).by_attribute()
    assert set(overlay) == set(full)
    for identity, spec in overlay.items():
        assert not spec.unique
        assert (spec.expression, spec.method, spec.value_type) == (
            full[identity].expression,
            full[identity].method,
            full[identity].value_type,
        )
        assert spec.predicate_sql(DOMAIN, "agent/x") == full[identity].predicate_sql(
            DOMAIN, "agent/x"
        )
        renamed = spec.name(DOMAIN, "agent/x") != full[identity].name(DOMAIN, "agent/x")
        assert renamed == full[identity].unique
    assert overlay[(SUPPLIER, VAT)].columns == f"(\"values\" ->> '{VAT}'), key"
    assert overlay[(PART, NOTES_TEXT)].digest


def test_ddl_quotes_literals_and_checks_identifiers() -> None:
    assert sql_literal("it's") == "'it''s'"
    with pytest.raises(ValueError, match="NUL"):
        sql_literal("a\x00b")
    assert predicate_sql("d'1", "b:x", SUPPLIER) == (
        f"domain_id = 'd''1' AND branch = 'b:x' AND kind_id = '{SUPPLIER}'"
    )
    (spec,) = plan_indexes(schema_of(supplier_kind())).indexes
    statement = create_index_sql(spec, DOMAIN, "main", "mat_records_p3", concurrently=True)
    assert statement.startswith(f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_PREFIX}")
    with pytest.raises(ValueError, match="identifier"):
        create_index_sql(spec, DOMAIN, "main", "mat_records; drop", concurrently=False)
    with pytest.raises(ValueError, match="schema element id"):
        predicate_sql(DOMAIN, "main", "knd_x'")


# Against Postgres -------------------------------------------------------------------------------

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
async def sessions(engine: AsyncEngine, runtime: StoreRuntime) -> AsyncIterator[Sessions]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await create_domain(session, domain(), author=AUTHOR, runtime=runtime, clock=Clock())
    yield factory
    async with factory() as session:
        report = await verify(session, DOMAIN, "main", runtime=runtime, deep=True)
    assert report.ok, report.divergences


async def indexes_of(sessions: Sessions, branch: str = "main") -> dict[str, tuple[str, bool]]:
    """Attribute id -> (index name, valid) for one branch."""
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        found = await existing_indexes(session, DOMAIN)
    return {i.attribute_id: (i.name, i.valid) for i in found if i.branch == branch}


async def commit_changes(
    sessions: Sessions, runtime: StoreRuntime, *changes: Any, schema: tuple[Kind, ...] = ()
) -> None:
    async with sessions() as session:
        await commit(
            session,
            DOMAIN,
            "main",
            list(changes),
            schema_changes=[SchemaUpsert(k) for k in schema],
            author=AUTHOR,
            message="change",
            origin=ORIGIN,
            runtime=runtime,
        )


pytestmark_integration = pytest.mark.integration


@pytestmark_integration
async def test_schema_change_creates_and_retires_indexes(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    async with sessions() as session:
        await commit(
            session,
            DOMAIN,
            "main",
            [supplier("S-1", "Alpha", vat="IT1"), supplier("S-2", "Beta", vat="IT2")],
            schema_changes=schema_changes(),
            author=AUTHOR,
            message="schema",
            origin=ORIGIN,
            runtime=runtime,
        )
    assert set(await indexes_of(sessions)) == {VAT}

    async with sessions() as session, session.begin():
        await session.execute(text("SET LOCAL lorebase.bypass_rls = 'on'"))
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    'UPDATE mat_records SET "values" = jsonb_set("values", :path, \'"IT1"\') '
                    "WHERE domain_id = :d AND key = 'S-2'"
                ),
                {"path": [VAT], "d": DOMAIN},
            )

    rated = with_attributes(
        supplier_kind(rating=T("integer")), **{VAT: {"unique": False}, RATING: {"indexed": True}}
    )
    await commit_changes(sessions, runtime, supplier("S-3", "Gamma", rating="7"), schema=(rated,))
    assert set(await indexes_of(sessions)) == {RATING}
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        jobs = (
            await session.execute(
                text("SELECT count(*) FROM jobs WHERE type = :t"), {"t": INDEX_JOB_TYPE}
            )
        ).scalar_one()
    assert jobs == 0, "a dropped unique index is retired inline, not queued"

    # The type of an indexed Attribute changes: the cast index goes before the rows change.
    retyped = with_attributes(rated, **{RATING: {"type": T("string")}})
    await commit_changes(
        sessions, runtime, supplier("S-4", "Delta", rating="not a number"), schema=(retyped,)
    )
    (name, valid) = (await indexes_of(sessions))[RATING]
    assert valid
    async with sessions() as session, session.begin():
        definition = (
            await session.execute(text("SELECT pg_get_indexdef(CAST(:n AS regclass))"), {"n": name})
        ).scalar_one()
    assert "bigint" not in definition
    assert f"kind_id = '{SUPPLIER}'" in definition


@pytestmark_integration
async def test_large_kinds_are_queued_and_built_concurrently(
    engine: AsyncEngine, sessions: Sessions
) -> None:
    runtime = StoreRuntime.from_settings(
        StoreSettings(indexes=IndexSettings(inline_max_rows=1, job_batch=5))
    )
    async with sessions() as session:
        await commit(
            session,
            DOMAIN,
            "main",
            [supplier("S-1", "Alpha", vat="IT1"), supplier("S-2", "Beta", vat="IT2")],
            schema_changes=schema_changes(),
            author=AUTHOR,
            message="schema",
            origin=ORIGIN,
            runtime=runtime,
        )
    assert await indexes_of(sessions) == {}
    outcomes = await run_index_jobs(engine, runtime.settings.indexes)
    assert [(o.action, o.status) for o in outcomes] == [("create", "succeeded")]
    assert [valid for _, valid in (await indexes_of(sessions)).values()] == [True]
    assert await run_index_jobs(engine, runtime.settings.indexes) == []


@pytestmark_integration
async def test_branch_creation_and_deletion(
    engine: AsyncEngine, sessions: Sessions, runtime: StoreRuntime
) -> None:
    async with sessions() as session:
        await commit(
            session,
            DOMAIN,
            "main",
            [supplier("S-1", "Alpha", vat="IT1")],
            schema_changes=schema_changes(),
            author=AUTHOR,
            message="schema",
            origin=ORIGIN,
            runtime=runtime,
        )
    async with sessions() as session:
        await create_branch(session, DOMAIN, "feature", created_by="usr_test", runtime=runtime)
    feature = await indexes_of(sessions, "feature")
    assert set(feature) == {VAT}
    assert feature[VAT] != (await indexes_of(sessions))[VAT]

    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        queued = await BranchIndexes(
            session, DOMAIN, "feature", settings=runtime.settings.indexes
        ).drop_all()
    assert queued.queued == [feature[VAT][0]]
    outcomes = await run_index_jobs(engine, runtime.settings.indexes)
    assert [o.status for o in outcomes] == ["cancelled"], "the branch still exists"

    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await session.execute(
            text("DELETE FROM refs WHERE domain_id = :d AND name = 'feature'"), {"d": DOMAIN}
        )
        await BranchIndexes(
            session, DOMAIN, "feature", settings=runtime.settings.indexes
        ).drop_all()
    outcomes = await run_index_jobs(engine, runtime.settings.indexes)
    assert [o.status for o in outcomes] == ["succeeded"]
    assert await indexes_of(sessions, "feature") == {}
    assert set(await indexes_of(sessions)) == {VAT}


@pytestmark_integration
async def test_overlay_indexes_are_queued_and_replaced_on_promotion(
    engine: AsyncEngine, sessions: Sessions, runtime: StoreRuntime
) -> None:
    async with sessions() as session:
        await commit(
            session,
            DOMAIN,
            "main",
            [supplier("S-1", "Alpha", vat="IT1")],
            schema_changes=schema_changes(),
            author=AUTHOR,
            message="schema",
            origin=ORIGIN,
            runtime=runtime,
        )
    overlay = "agent/indexed"
    async with sessions() as session:
        await create_branch(
            session, DOMAIN, overlay, created_by="usr_test", mode="overlay", runtime=runtime
        )
    assert await indexes_of(sessions, overlay) == {}, "an overlay's indexes are queued"
    outcomes = await run_index_jobs(engine, runtime.settings.indexes)
    assert [(o.action, o.status) for o in outcomes] == [("create", "succeeded")]
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        found = [i for i in await existing_indexes(session, DOMAIN) if i.branch == overlay]
        assert await pending_index_plan(session, DOMAIN, branch=overlay) == []
    assert [(i.attribute_id, i.unique, i.valid) for i in found] == [(VAT, False, True)]

    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        assert await promote(session, DOMAIN, overlay, runtime=runtime)
    outcomes = await run_index_jobs(engine, runtime.settings.indexes)
    assert [(o.action, o.status) for o in outcomes] == [("drop", "succeeded")]
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        found = [i for i in await existing_indexes(session, DOMAIN) if i.branch == overlay]
    assert [(i.attribute_id, i.unique, i.valid) for i in found] == [(VAT, True, True)]


@pytestmark_integration
async def test_roles_that_do_not_own_mat_records_skip_index_management(
    engine: AsyncEngine, sessions: Sessions, runtime: StoreRuntime
) -> None:
    role = f"lorebase_writer_{uuid.uuid4().hex[:8]}"
    async with engine.connect() as connection:
        auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await auto.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN")
        await auto.exec_driver_sql(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {role}")
    try:
        async with sessions() as session, session.begin():
            await session.execute(text(f"SET LOCAL ROLE {role}"))
            await scope_to_domain(session, DOMAIN)
            indexes = BranchIndexes(session, DOMAIN, "main", settings=runtime.settings.indexes)
            synced = await indexes.sync(schema_of(supplier_kind()))
        assert (synced.created, synced.dropped, synced.queued) == ([], [], [])
    finally:
        async with engine.connect() as connection:
            auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await auto.exec_driver_sql(f"DROP OWNED BY {role}")
            await auto.exec_driver_sql(f"DROP ROLE {role}")
    assert await indexes_of(sessions) == {}


@asynccontextmanager
async def writer_role(engine: AsyncEngine) -> AsyncIterator[str]:
    """A role that commits but does not own mat_records, like the application role."""
    role = f"lorebase_writer_{uuid.uuid4().hex[:8]}"
    async with engine.connect() as connection:
        auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await auto.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN")
        await auto.exec_driver_sql(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}"
        )
        await auto.exec_driver_sql(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {role}")
    try:
        yield role
    finally:
        async with engine.connect() as connection:
            auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await auto.exec_driver_sql(f"DROP OWNED BY {role}")
            await auto.exec_driver_sql(f"DROP ROLE {role}")


async def commit_as(
    sessions: Sessions, role: str, runtime: StoreRuntime, *changes: Any, schema: Any = ()
) -> None:
    async with sessions() as session, session.begin():
        await session.execute(text(f"SET LOCAL ROLE {role}"))
        await commit(
            session,
            DOMAIN,
            "main",
            list(changes),
            schema_changes=list(schema),
            author=AUTHOR,
            message="change",
            origin=ORIGIN,
            runtime=runtime,
        )


async def queued_jobs(sessions: Sessions) -> list[dict[str, Any]]:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        found = await session.execute(
            text("SELECT payload FROM jobs WHERE type = :t AND status = 'queued' ORDER BY id"),
            {"t": INDEX_JOB_TYPE},
        )
        return [dict(payload) for payload in found.scalars()]


@pytestmark_integration
async def test_a_maintenance_role_builds_the_indexes_queued_by_the_application_role(
    engine: AsyncEngine, sessions: Sessions, database_url: str
) -> None:
    """With `database.maintenance_dsn`, a commit role that does not own mat_records queues
    its index changes; the job worker runs them as the maintenance role (here the test's
    owner engine)."""
    database = DatabaseSettings(maintenance_dsn=database_url)
    runtime = StoreRuntime.from_settings(StoreSettings(), database=database)
    assert runtime.index_maintenance
    worker = {INDEX_JOB: lambda job: run_index_job(engine, job)}
    async with writer_role(engine) as role:
        await commit_as(
            sessions,
            role,
            runtime,
            supplier("S-1", "Alpha", vat="IT1"),
            supplier("S-2", "Beta", vat="IT2"),
            schema=schema_changes(),
        )
        assert await indexes_of(sessions) == {}
        assert [job["action"] for job in await queued_jobs(sessions)] == ["create"]
        assert await run_jobs(sessions, worker) == 1
        (name, valid) = (await indexes_of(sessions))[VAT]
        assert valid

        # VAT stops being unique and a duplicate arrives in the same commit: the unique index
        # rejects it, so its drop is queued (forced) in a transaction of its own; the commit
        # succeeds once the worker has dropped it.
        relaxed = with_attributes(supplier_kind(), **{VAT: {"unique": False}})
        duplicate = supplier("S-3", "Gamma", vat="IT1")
        with pytest.raises(IntegrityError):
            await commit_as(sessions, role, runtime, duplicate, schema=(SchemaUpsert(relaxed),))
        assert [(j["action"], j["name"], j.get("force")) for j in await queued_jobs(sessions)] == [
            ("drop", name, True)
        ]
        assert await run_jobs(sessions, worker) == 1
        await commit_as(sessions, role, runtime, duplicate, schema=(SchemaUpsert(relaxed),))
        assert await indexes_of(sessions) == {}
        assert await queued_jobs(sessions) == []


@pytestmark_integration
async def test_the_pending_plan_prints_the_ddl_an_administrator_applies(
    engine: AsyncEngine, sessions: Sessions, database_url: str
) -> None:
    """Without ownership or a maintenance role nothing is built; `python -m
    lorebase_store.indexes plan` prints the DDL, and applying it leaves nothing pending."""
    async with writer_role(engine) as role:
        await commit_as(
            sessions,
            role,
            StoreRuntime.from_settings(StoreSettings()),
            supplier("S-1", "Alpha", vat="IT1"),
            schema=schema_changes(),
        )
    assert await indexes_of(sessions) == {}
    assert await queued_jobs(sessions) == []

    environment = {
        **{k: v for k, v in os.environ.items() if not k.startswith("LOREBASE")},
        "LOREBASE__DATABASE__DSN": database_url,
    }
    done = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "lorebase_store.indexes", "plan", "--domain", DOMAIN],
        capture_output=True,
        text=True,
        env=environment,
        check=True,
    )
    statements = [line.removesuffix(";") for line in done.stdout.splitlines()]
    assert len(statements) == 2
    assert statements[0].startswith("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ")
    assert f"kind_id = '{SUPPLIER}'" in statements[0]
    assert statements[1].startswith("COMMENT ON INDEX ")

    async with engine.connect() as connection:
        auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
        for statement in statements:
            await auto.exec_driver_sql(statement)
    assert [valid for _, valid in (await indexes_of(sessions)).values()] == [True]
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        assert await pending_index_plan(session, DOMAIN) == []
        assert await pending_index_plan(session, DOMAIN, branch="nope") == []


@pytestmark_integration
async def test_a_drop_job_only_drops_managed_indexes_of_its_domain(
    engine: AsyncEngine, sessions: Sessions
) -> None:
    # The application role may queue jobs; the maintenance role runs them. A queued name must
    # not let the first drop what the second owns beyond this Domain's managed indexes.
    job = JobRow(
        id=1,
        type=INDEX_JOB,
        domain_id=DOMAIN,
        payload={"action": "drop", "name": "ix_jobs_claimable", "branch": "main"},
        priority=0,
        attempts=0,
        run_after=datetime.now(UTC),
        locked_by=None,
        created_at=datetime.now(UTC),
    )
    result = await run_index_job(engine, job)
    assert result["status"] == "cancelled"
    async with engine.connect() as connection:
        found = await connection.execute(text("SELECT to_regclass('ix_jobs_claimable')"))
        assert found.scalar_one() is not None
