"""Garbage collection: dry run, sweep, grace period, races with commits, jobs."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import Kind, KindSearch
from lorebase_store.commit import CommitResult, SchemaUpsert, commit, create_domain
from lorebase_store.db import create_engine
from lorebase_store.embeddings import NewVector, store_vectors, template_fingerprints
from lorebase_store.gc import (
    PHASE_MARKED,
    GcBusyError,
    GcError,
    GcReport,
    collect_garbage,
    enqueue_gc,
    gc_lock_key,
    run_gc_job,
)
from lorebase_store.gc import main as gc_main
from lorebase_store.jobs import claim_jobs, requeue_stale, run_jobs
from lorebase_store.materialize import create_branch, verify
from lorebase_store.models.enums import RefKind
from lorebase_store.repositories import RefRepository, scope_to_domain
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, GcSettings, JobSettings, StoreSettings

from .support import (
    AUTHOR,
    DOMAIN,
    ORIGIN,
    SUPPLIER,
    Clock,
    domain,
    part,
    part_kind,
    schema_changes,
    supplied_by,
    supplier,
    supplier_kind,
)

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]

LATER = datetime(2026, 10, 25, tzinfo=UTC)
"""A time long after every test commit: with the default grace period nothing is young."""

COUNTS = {
    "revisions": "SELECT count(*) FROM revisions",
    "changesets": "SELECT count(*) FROM changesets",
    "nodes": "SELECT count(*) FROM tree_nodes",
    "objects": "SELECT count(*) FROM objects",
    "provenance": "SELECT count(*) FROM provenance",
}


@pytest.fixture
async def sessions(engine: AsyncEngine, runtime: StoreRuntime) -> Sessions:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await create_domain(session, domain(), author=AUTHOR, runtime=runtime, clock=CLOCK)
    return factory


CLOCK = Clock()


@pytest.fixture(autouse=True)
def fresh_clock() -> None:
    global CLOCK
    CLOCK = Clock()


async def put(
    sessions: Sessions, runtime: StoreRuntime, changes: list[Any], branch: str = "main", **kw: Any
) -> CommitResult:
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
            clock=CLOCK,
            **kw,
        )


async def counts(sessions: Sessions) -> dict[str, int]:
    async with sessions() as session:
        return {
            name: int((await session.execute(text(sql))).scalar_one())
            for name, sql in COUNTS.items()
        }


async def seed_main(sessions: Sessions, runtime: StoreRuntime) -> None:
    await put(
        sessions,
        runtime,
        [supplier("S-1", "Acme", country="IT"), part("P-1", "Bearing")],
        schema_changes=schema_changes(),
    )


DEV_CHANGES = [supplier("S-9", "Zed Works", country="DE"), part("P-9", "Washer")]


async def branch_with_work(sessions: Sessions, runtime: StoreRuntime) -> CommitResult:
    """Branch `dev` off main with one commit of unique content."""
    async with sessions() as session:
        await create_branch(
            session, DOMAIN, "dev", created_by=AUTHOR.principal, runtime=runtime, clock=CLOCK
        )
    return await put(sessions, runtime, list(DEV_CHANGES), branch="dev")


async def delete_ref(sessions: Sessions, name: str) -> None:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        assert await RefRepository(session, DOMAIN).delete(name)


async def assert_main_intact(sessions: Sessions, runtime: StoreRuntime) -> None:
    async with sessions() as session:
        report = await verify(session, DOMAIN, "main", runtime=runtime, deep=True)
    assert report.ok, report.divergences
    assert report.rows > 0


def deleted(report: GcReport) -> dict[str, int]:
    return {
        "revisions": report.revisions,
        "changesets": report.changesets,
        "nodes": report.nodes,
        "objects": report.objects,
    }


async def test_dry_run_reports_then_gc_deletes_unreachable(
    engine: AsyncEngine, sessions: Sessions, runtime: StoreRuntime
) -> None:
    await seed_main(sessions, runtime)
    before_branch = await counts(sessions)
    dev = await branch_with_work(sessions, runtime)
    with_dev = await counts(sessions)
    assert with_dev["revisions"] == before_branch["revisions"] + 1

    # Nothing is garbage while the branch exists.
    quiet = await collect_garbage(engine, DOMAIN, runtime=runtime, dry_run=True, now=LATER)
    assert deleted(quiet) == dict.fromkeys(deleted(quiet), 0)
    assert quiet.marked_revisions == with_dev["revisions"]

    await delete_ref(sessions, "dev")
    preview = await collect_garbage(engine, DOMAIN, runtime=runtime, dry_run=True, now=LATER)
    assert preview.dry_run
    assert preview.revisions == 1
    assert preview.changesets == len(dev.changeset)
    assert preview.nodes > 0
    assert preview.objects == 2
    assert preview.node_bytes > 0
    assert preview.object_bytes > 0
    assert await counts(sessions) == with_dev  # dry run changes nothing

    report = await collect_garbage(engine, DOMAIN, runtime=runtime, now=LATER)
    assert not report.dry_run
    assert deleted(report) == deleted(preview)
    assert (report.node_bytes, report.object_bytes) == (preview.node_bytes, preview.object_bytes)
    after = await counts(sessions)
    assert after["revisions"] == with_dev["revisions"] - 1
    assert after["changesets"] == with_dev["changesets"] - preview.changesets
    assert after["nodes"] == with_dev["nodes"] - preview.nodes
    assert after["objects"] == with_dev["objects"] - preview.objects
    assert after["provenance"] == with_dev["provenance"] - report.provenance
    assert report.provenance >= 1

    # Survivors verify, nothing reachable is missing, and a second pass finds nothing.
    await assert_main_intact(sessions, runtime)
    again = await collect_garbage(engine, DOMAIN, runtime=runtime, dry_run=True, now=LATER)
    assert deleted(again) == dict.fromkeys(deleted(again), 0)
    assert again.missing_nodes == 0
    await put(sessions, runtime, [part("P-2", "Nut")])
    await assert_main_intact(sessions, runtime)


async def test_young_revisions_are_kept_for_the_grace_period(
    engine: AsyncEngine, sessions: Sessions, runtime: StoreRuntime
) -> None:
    await seed_main(sessions, runtime)
    dev = await branch_with_work(sessions, runtime)
    await delete_ref(sessions, "dev")
    snapshot = await counts(sessions)

    young = await collect_garbage(engine, DOMAIN, runtime=runtime, now=dev.created_at)
    assert deleted(young) == dict.fromkeys(deleted(young), 0)
    assert await counts(sessions) == snapshot

    soon = dev.created_at + timedelta(seconds=3599)
    assert (await collect_garbage(engine, DOMAIN, runtime=runtime, now=soon)).revisions == 0
    past = dev.created_at + timedelta(seconds=3601)
    assert (await collect_garbage(engine, DOMAIN, runtime=runtime, now=past)).revisions == 1
    # An explicit grace of zero collects at once.
    await seed_second_dev(sessions, runtime)
    now = CLOCK()
    assert (
        await collect_garbage(engine, DOMAIN, runtime=runtime, now=now, grace_seconds=0)
    ).revisions == 1


async def seed_second_dev(sessions: Sessions, runtime: StoreRuntime) -> None:
    async with sessions() as session:
        await create_branch(
            session, DOMAIN, "dev2", created_by=AUTHOR.principal, runtime=runtime, clock=CLOCK
        )
    await put(sessions, runtime, [part("P-7", "Spacer")], branch="dev2")
    await delete_ref(sessions, "dev2")


async def test_tags_keep_their_revisions(
    engine: AsyncEngine, sessions: Sessions, runtime: StoreRuntime
) -> None:
    await seed_main(sessions, runtime)
    dev = await branch_with_work(sessions, runtime)
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await RefRepository(session, DOMAIN).create(
            "release/1",
            dev.revision_id,
            created_at=CLOCK(),
            created_by=AUTHOR.principal,
            kind=RefKind.TAG,
        )
    await delete_ref(sessions, "dev")
    report = await collect_garbage(engine, DOMAIN, runtime=runtime, now=LATER)
    assert deleted(report) == dict.fromkeys(deleted(report), 0)
    async with sessions() as session:
        assert (await verify(session, DOMAIN, "main", runtime=runtime)).ok


async def test_open_merge_attempts_keep_their_revisions(
    engine: AsyncEngine, sessions: Sessions, runtime: StoreRuntime
) -> None:
    await seed_main(sessions, runtime)
    dev = await branch_with_work(sessions, runtime)
    await delete_ref(sessions, "dev")
    async with sessions() as session, session.begin():
        await session.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
        await session.execute(
            text(
                "INSERT INTO proposals (domain_id, id, title, source_ref, target_ref, origin, "
                "status, author, created_at) VALUES (:d, 'prp_1', 'x', 'dev', 'main', 'manual', "
                "'open', 'usr_test', :at)"
            ),
            {"d": DOMAIN, "at": CLOCK()},
        )
        await session.execute(
            text(
                "INSERT INTO merge_attempts (domain_id, id, proposal_id, base, ours, theirs, "
                "status, created_at) VALUES (:d, 'mat_1', 'prp_1', :b, :o, :t, 'conflicted', :at)"
            ),
            {
                "d": DOMAIN,
                "b": dev.parents[0],
                "o": dev.parents[0],
                "t": dev.revision_id,
                "at": CLOCK(),
            },
        )
    kept = await collect_garbage(engine, DOMAIN, runtime=runtime, now=LATER)
    assert deleted(kept) == dict.fromkeys(deleted(kept), 0)

    async with sessions() as session, session.begin():
        await session.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
        await session.execute(text("UPDATE proposals SET status = 'closed'"))
    freed = await collect_garbage(engine, DOMAIN, runtime=runtime, now=LATER)
    assert freed.revisions == 1


async def test_commit_landing_after_marking_keeps_reused_objects(
    engine: AsyncEngine, sessions: Sessions, runtime: StoreRuntime
) -> None:
    """The same content committed to main after the marking pass is found again by the sweep."""
    await seed_main(sessions, runtime)
    await branch_with_work(sessions, runtime)
    await delete_ref(sessions, "dev")

    async def commit_during_gc(phase: str) -> None:
        if phase == PHASE_MARKED:
            await put(sessions, runtime, list(DEV_CHANGES))

    report = await collect_garbage(
        engine, DOMAIN, runtime=runtime, now=LATER, hook=commit_during_gc
    )
    assert report.revisions == 1  # the dead dev Revision itself goes
    assert report.objects == 0  # but main's new commit holds its objects
    assert report.nodes == 0
    await assert_main_intact(sessions, runtime)
    final = await collect_garbage(engine, DOMAIN, runtime=runtime, dry_run=True, now=LATER)
    assert final.missing_nodes == 0


async def test_commit_in_flight_holds_the_sweep_until_it_ends(
    engine: AsyncEngine, sessions: Sessions, runtime: StoreRuntime
) -> None:
    await seed_main(sessions, runtime)
    await branch_with_work(sessions, runtime)
    await delete_ref(sessions, "dev")

    committing = sessions()
    await committing.begin()
    await commit(
        committing,
        DOMAIN,
        "main",
        list(DEV_CHANGES),
        author=AUTHOR,
        message="reuse",
        origin=ORIGIN,
        runtime=runtime,
        clock=CLOCK,
    )
    collection = asyncio.create_task(collect_garbage(engine, DOMAIN, runtime=runtime, now=LATER))
    await asyncio.sleep(1.0)
    assert not collection.done()  # waiting for main's branch lock
    await committing.commit()
    await committing.close()
    report = await asyncio.wait_for(collection, timeout=30)
    assert (report.revisions, report.objects, report.nodes) == (1, 0, 0)
    await assert_main_intact(sessions, runtime)


async def test_collection_stops_when_commits_hold_locks_too_long(
    engine: AsyncEngine, sessions: Sessions, store: StoreSettings
) -> None:
    runtime = StoreRuntime.from_settings(
        store.model_copy(update={"gc": GcSettings(guard_wait_seconds=0.5, guard_retry_ms=50)})
    )
    await seed_main(sessions, runtime)
    await branch_with_work(sessions, runtime)
    await delete_ref(sessions, "dev")
    snapshot = await counts(sessions)
    committing = sessions()
    await committing.begin()
    await commit(
        committing,
        DOMAIN,
        "main",
        [part("P-5", "Pin")],
        author=AUTHOR,
        message="slow",
        origin=ORIGIN,
        runtime=runtime,
        clock=CLOCK,
    )
    with pytest.raises(GcError, match="branch locks"):
        await collect_garbage(engine, DOMAIN, runtime=runtime, now=LATER)
    await committing.rollback()
    await committing.close()
    assert await counts(sessions) == snapshot
    assert (await collect_garbage(engine, DOMAIN, runtime=runtime, now=LATER)).revisions == 1


async def test_second_collection_of_a_domain_is_refused(
    engine: AsyncEngine, sessions: Sessions, runtime: StoreRuntime
) -> None:
    await seed_main(sessions, runtime)
    async with engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": gc_lock_key(DOMAIN)})
        with pytest.raises(GcBusyError):
            await collect_garbage(engine, DOMAIN, runtime=runtime, now=LATER)
        await holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": gc_lock_key(DOMAIN)})
        await holder.commit()
    assert (await collect_garbage(engine, DOMAIN, runtime=runtime, now=LATER)).revisions == 0


async def test_gc_job_is_claimed_once_and_runs(
    engine: AsyncEngine, sessions: Sessions, runtime: StoreRuntime
) -> None:
    await seed_main(sessions, runtime)
    await branch_with_work(sessions, runtime)
    await delete_ref(sessions, "dev")

    async with sessions() as session, session.begin():
        first = await enqueue_gc(session, DOMAIN, grace_seconds=0)
        assert await enqueue_gc(session, DOMAIN, grace_seconds=0) == first  # no duplicate
        second = await enqueue_gc(session, DOMAIN, dry_run=True)
    assert second != first

    # Two workers claiming at once never receive the same job.
    one, two = sessions(), sessions()
    await one.begin()
    await two.begin()
    claimed_one = await claim_jobs(one, "w1", types=["gc"], limit=1)
    claimed_two = await claim_jobs(two, "w2", types=["gc"], limit=2)
    await one.commit()
    await two.commit()
    await one.close()
    await two.close()
    ids = [job.id for job in [*claimed_one, *claimed_two]]
    assert sorted(ids) == sorted([first, second])
    assert len(ids) == len(set(ids))

    async with sessions() as session, session.begin():
        assert await requeue_stale(session, settings=JobSettings(stale_after_seconds=1e-6)) == 2
    ran = await run_jobs(
        sessions,
        {"gc": lambda job: run_gc_job(engine, job, runtime=runtime)},
        worker_id="w3",
    )
    assert ran == 2
    async with sessions() as session, session.begin():
        await session.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
        rows = (
            await session.execute(text("SELECT id, status::text, result FROM jobs ORDER BY id"))
        ).all()
    assert [row[1] for row in rows] == ["succeeded", "succeeded"]
    by_id = {row[0]: row[2] for row in rows}
    assert by_id[first]["revisions"] == 1
    assert by_id[second]["dry_run"] is True


async def test_failing_job_is_retried_then_failed(sessions: Sessions) -> None:
    async with sessions() as session, session.begin():
        await session.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
        await session.execute(text("INSERT INTO jobs (type) VALUES ('boom')"))
    calls = 0

    async def boom(_: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise RuntimeError("nope")

    settings = JobSettings(max_attempts=2, retry_delay_seconds=0)
    for _ in range(3):
        await run_jobs(sessions, {"boom": boom}, settings=settings)
    assert calls == 2
    async with sessions() as session, session.begin():
        await session.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
        status, result = (
            await session.execute(text("SELECT status::text, result FROM jobs"))
        ).one()
    assert status == "failed"
    assert "RuntimeError: nope" in result["error"]


def test_command_line_reports_and_rejects_unknown_domains(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def seed() -> None:
        engine = create_engine(DatabaseSettings(), url=database_url, pooled=False)
        try:
            async with async_sessionmaker(engine)() as session:
                await create_domain(
                    session,
                    domain(),
                    author=AUTHOR,
                    runtime=StoreRuntime.from_settings(StoreSettings()),
                    clock=Clock(),
                )
        finally:
            await engine.dispose()

    asyncio.run(seed())
    monkeypatch.setenv("LOREBASE__DATABASE__DSN", database_url)
    assert gc_main(["--domain", DOMAIN, "--dry-run", "--grace-seconds", "0"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["domain_id"], report["dry_run"], report["revisions"]) == (DOMAIN, True, 0)
    assert report["marked_revisions"] == 1
    assert gc_main(["--domain", "nope"]) == 1
    assert "nope" in capsys.readouterr().err


VectorKey = tuple[bytes, str]


def templated(supplier_template: str = "{legal_name}") -> tuple[Kind, Kind]:
    """Supplier and Part with embedding templates."""
    supplier_with = supplier_kind(search=KindSearch(embedding_template=supplier_template))
    plain = part_kind()
    part_with = plain.model_copy(update={"search": KindSearch(embedding_template="{name}")})
    return supplier_with, part_with


async def vector_keys(sessions: Sessions) -> set[VectorKey]:
    async with sessions() as session:
        rows = await session.execute(text("SELECT object_hash, fingerprint FROM embeddings"))
        return {(bytes(h), str(f)) for h, f in rows.all()}


async def test_unreferenced_vectors_are_counted_then_deleted(
    engine: AsyncEngine, sessions: Sessions, runtime: StoreRuntime
) -> None:
    kinds = templated()
    await put(
        sessions,
        runtime,
        [supplier("S-1", "Acme", country="IT"), part("P-1", "Bearing")],
        schema_changes=[SchemaUpsert(k) for k in (*kinds, supplied_by())],
    )
    await branch_with_work(sessions, runtime)
    fingerprints = template_fingerprints(kinds)
    async with sessions() as session:
        rows = await session.execute(
            text("SELECT branch, kind_id, object_hash FROM mat_records WHERE domain_id = :d"),
            {"d": DOMAIN},
        )
        by_branch: dict[str, set[VectorKey]] = {}
        for branch, kind_id, object_hash in rows.all():
            by_branch.setdefault(branch, set()).add((bytes(object_hash), fingerprints[kind_id]))
    only_dev = by_branch["dev"] - by_branch["main"]
    assert only_dev
    held = next(iter(by_branch["main"]))
    stray = (b"\x07" * 32, fingerprints[SUPPLIER])
    stale = (held[0], "a-template-no-branch-uses")
    dims = runtime.settings.embeddings.dims
    keys = sorted(by_branch["main"] | only_dev | {stray, stale})
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await store_vectors(
            session,
            DOMAIN,
            model="m",
            vectors=[NewVector(h, f, h, [0.5] * dims) for h, f in keys],
        )
    assert await vector_keys(sessions) == set(keys)

    # Every vector but the stray one and the one of a template no branch uses is referenced.
    quiet = await collect_garbage(engine, DOMAIN, runtime=runtime, dry_run=True, now=LATER)
    assert quiet.embeddings == 2

    # A new Supplier template on dev: the vector of its own Supplier under the old template
    # is no longer referenced (main does not hold that Record).
    supplier_now, _ = templated("{legal_name} ({country})")
    await put(sessions, runtime, [], branch="dev", schema_changes=[SchemaUpsert(supplier_now)])
    replaced = {(h, f) for h, f in only_dev if f == fingerprints[SUPPLIER]}
    assert replaced
    changed = await collect_garbage(engine, DOMAIN, runtime=runtime, dry_run=True, now=LATER)
    assert changed.embeddings == 2 + len(replaced)

    await delete_ref(sessions, "dev")
    preview = await collect_garbage(engine, DOMAIN, runtime=runtime, dry_run=True, now=LATER)
    assert preview.embeddings == 2 + len(only_dev)
    assert await vector_keys(sessions) == set(keys)  # dry run changes nothing

    report = await collect_garbage(engine, DOMAIN, runtime=runtime, now=LATER)
    assert report.embeddings == preview.embeddings
    assert await vector_keys(sessions) == by_branch["main"]
    again = await collect_garbage(engine, DOMAIN, runtime=runtime, dry_run=True, now=LATER)
    assert again.embeddings == 0
