"""Domain bundles: export, tamper detection and a round trip into a second database."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from lorebase_core.tree import DiffEntry, tree_diff
from lorebase_store import migrations, testing
from lorebase_store.bundle import (
    BundleError,
    DomainNotEmptyError,
    export_domain,
    import_bundle,
    read_manifest,
)
from lorebase_store.bundle import main as bundle_main
from lorebase_store.commit import create_domain
from lorebase_store.db import create_engine
from lorebase_store.gc import collect_garbage
from lorebase_store.materialize import create_branch, verify
from lorebase_store.models.enums import Materialization, RefKind
from lorebase_store.nodestore import PgNodeStore
from lorebase_store.repositories import DomainNotFoundError, RefRepository, scope_to_domain
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import BundleSettings, DatabaseSettings, StoreSettings

from .support import AUTHOR, DOMAIN, Clock, domain, part, schema_changes
from .test_materialize import history_of_commits, run, snapshot

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]

TABLES = {
    "revisions": (
        "SELECT id, root, generation, author, on_behalf_of, message, origin, stats, created_at "
        "FROM revisions ORDER BY generation, id"
    ),
    "parents": (
        "SELECT revision_id, position, parent_id FROM revision_parents "
        "ORDER BY revision_id, position"
    ),
    "changesets": (
        "SELECT revision_id, partition, element_key, old_hash, new_hash, provenance "
        "FROM changesets ORDER BY revision_id, partition, element_key"
    ),
    "provenance": "SELECT id, origin_type, origin_ref, actor, at FROM provenance ORDER BY id",
    "refs": (
        "SELECT name, kind::text, head, protected, materialization::text, version, created_at, "
        "created_by FROM refs ORDER BY name"
    ),
    "ref_log": (
        "SELECT ref_name, seq, old_head, new_head, reason, actor, at FROM ref_log "
        "ORDER BY ref_name, seq"
    ),
    "domain": (
        "SELECT id, display_name, description, default_branch, leaf_size, settings, created_at, "
        "created_by FROM domains"
    ),
}


@pytest.fixture
async def sessions(engine: AsyncEngine, runtime: StoreRuntime) -> Sessions:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await create_domain(session, domain(), author=AUTHOR, runtime=runtime, clock=Clock())
    return factory


@pytest.fixture
def target_url(
    postgres_server_url: str, database: DatabaseSettings, store: StoreSettings
) -> Iterator[str]:
    """A second, migrated, empty database: where bundles are imported."""
    with testing.scratch_database(postgres_server_url) as url:
        migrations.upgrade(database=database, store=store, url=url)
        yield url


@pytest.fixture
async def target(target_url: str, database: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(database, url=target_url, pooled=False)
    try:
        yield engine
    finally:
        await engine.dispose()


async def populate(sessions: Sessions, runtime: StoreRuntime) -> Clock:
    """A history on main plus branches, a tag and a deleted branch holding garbage."""
    clock = Clock()
    commits = await history_of_commits(sessions, runtime, clock)
    async with sessions() as session:
        await create_branch(session, DOMAIN, "dev", created_by=AUTHOR.principal, runtime=runtime)
    await run(sessions, runtime, clock, [part("P-9", "Washer")], branch="dev")
    async with sessions() as session:
        await create_branch(
            session,
            DOMAIN,
            "audit/first",
            created_by=AUTHOR.principal,
            start=commits[0].revision_id,
            runtime=runtime,
        )
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        refs = RefRepository(session, DOMAIN)
        await refs.create(
            "release/1",
            commits[1].revision_id,
            created_at=clock(),
            created_by=AUTHOR.principal,
            kind=RefKind.TAG,
            materialization=Materialization.NONE,
        )
    async with sessions() as session:
        await create_branch(session, DOMAIN, "scratch", created_by="usr_t", runtime=runtime)
    await run(sessions, runtime, clock, [part("P-77", "Garbage")], branch="scratch")
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await RefRepository(session, DOMAIN).delete("scratch")
    return clock


async def table_rows(engine: AsyncEngine) -> dict[str, list[tuple[Any, ...]]]:
    async with async_sessionmaker(engine)() as session:
        await session.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', false)"))
        return {
            name: [tuple(row) for row in await session.execute(text(sql))]
            for name, sql in TABLES.items()
        }


async def hashes(engine: AsyncEngine, table: str) -> set[bytes]:
    async with async_sessionmaker(engine)() as session:
        await session.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', false)"))
        return {bytes(r[0]) for r in await session.execute(text(f"SELECT hash FROM {table}"))}


async def diff_between(engine: AsyncEngine, branch_a: str, branch_b: str) -> list[DiffEntry]:
    async with async_sessionmaker(engine)() as session:
        await scope_to_domain(session, DOMAIN)
        refs = RefRepository(session, DOMAIN)
        roots = []
        for name in (branch_a, branch_b):
            head = (await refs.require(name)).head
            roots.append(
                (
                    await session.execute(
                        text("SELECT root FROM revisions WHERE id = :h"), {"h": head}
                    )
                ).scalar_one()
            )

        def compute(sync: Session) -> list[DiffEntry]:
            return list(tree_diff(PgNodeStore(sync, DOMAIN), bytes(roots[0]), bytes(roots[1])))

        return await session.run_sync(compute)


async def test_round_trip_into_a_second_database(
    engine: AsyncEngine,
    target: AsyncEngine,
    sessions: Sessions,
    runtime: StoreRuntime,
    tmp_path: Path,
) -> None:
    await populate(sessions, runtime)
    garbage = await collect_garbage(engine, DOMAIN, runtime=runtime, dry_run=True, grace_seconds=0)
    assert garbage.revisions == 1

    bundle = tmp_path / "bundle"
    exported = await export_domain(engine, DOMAIN, bundle, runtime=runtime)
    assert exported.revisions > 5
    assert exported.refs == 4
    assert exported.schemas == 3  # branches main, dev, audit/first; the tag has none

    manifest = read_manifest(bundle)
    assert manifest["domain_id"] == DOMAIN
    assert manifest["format_version"] == 1
    assert manifest["compression"] == "gzip"
    assert manifest["lorebase_version"]
    assert manifest["counts"]["revisions"] == exported.revisions
    assert set(manifest["heads"]) == {"main", "dev", "audit/first", "release/1"}
    assert "revisions.jsonl.gz" in manifest["files"]
    assert "schemas/audit%2Ffirst.yaml" in manifest["files"]
    assert "Supplier" in (bundle / "schemas" / "main.yaml").read_text()

    imported = await import_bundle(target, bundle, runtime=runtime)
    assert imported.revisions == exported.revisions
    assert sorted(imported.rebuilt_branches) == ["audit/first", "dev", "main"]

    # Every Revision id and root, parent edge, changeset, ref and log entry is identical; the
    # deleted branch's Revision and its objects are not in the bundle.
    source_rows = await table_rows(engine)
    target_rows = await table_rows(target)
    source_revisions = source_rows["revisions"]
    assert len(source_revisions) == len(target_rows["revisions"]) + 1
    kept = {row[0] for row in target_rows["revisions"]}
    assert kept <= {row[0] for row in source_revisions}
    for name in ("domain", "refs", "ref_log", "provenance"):
        assert target_rows[name] == source_rows[name], name
    for row in target_rows["revisions"]:
        assert row in source_revisions
    assert target_rows["parents"] == [r for r in source_rows["parents"] if r[0] in kept]
    assert target_rows["changesets"] == [r for r in source_rows["changesets"] if r[0] in kept]

    source_nodes, target_nodes = (
        await hashes(engine, "tree_nodes"),
        await hashes(target, "tree_nodes"),
    )
    source_objects, target_objects = (
        await hashes(engine, "objects"),
        await hashes(target, "objects"),
    )
    assert target_nodes < source_nodes
    assert target_objects < source_objects
    assert len(source_nodes) - len(target_nodes) == garbage.nodes
    assert len(source_objects) - len(target_objects) == garbage.objects

    # Materialized heads are rebuilt equal to the source's, and verify.
    for branch in ("main", "dev", "audit/first"):
        async with async_sessionmaker(target)() as session:
            report = await verify(session, DOMAIN, branch, runtime=runtime, deep=True)
        assert report.ok, report.divergences
        assert report.rows > 0
        assert await snapshot(async_sessionmaker(target), branch) == await snapshot(
            sessions, branch
        )

    # Diffs between Revisions are the same on both sides.
    for pair in (("main", "audit/first"), ("main", "dev"), ("audit/first", "dev")):
        assert await diff_between(target, *pair) == await diff_between(engine, *pair)
    assert await diff_between(target, "main", "audit/first")

    # A Domain that already has content refuses a second import.
    with pytest.raises(DomainNotEmptyError):
        await import_bundle(target, bundle, runtime=runtime)


async def test_tarball_and_uncompressed_round_trip(
    engine: AsyncEngine,
    target: AsyncEngine,
    sessions: Sessions,
    store: StoreSettings,
    tmp_path: Path,
) -> None:
    runtime = StoreRuntime.from_settings(
        store.model_copy(update={"bundle": BundleSettings(compression="none", batch_rows=7)})
    )
    await populate(sessions, runtime)
    archive = tmp_path / "domain.tar"
    exported = await export_domain(engine, DOMAIN, archive, runtime=runtime)
    assert archive.is_file()
    with pytest.raises(BundleError, match="exists"):
        await export_domain(engine, DOMAIN, archive, runtime=runtime)
    await import_bundle(target, archive, runtime=runtime)
    source_rows, target_rows = await table_rows(engine), await table_rows(target)
    assert len(target_rows["revisions"]) == exported.revisions
    assert target_rows["refs"] == source_rows["refs"]
    assert {r[0] for r in target_rows["revisions"]} <= {r[0] for r in source_rows["revisions"]}


async def exported_copy(
    engine: AsyncEngine, runtime: StoreRuntime, source: Path, name: str
) -> Path:
    await export_domain(engine, DOMAIN, source / name, runtime=runtime)
    return source / name


async def domain_rows(engine: AsyncEngine) -> int:
    async with async_sessionmaker(engine)() as session:
        return int((await session.execute(text("SELECT count(*) FROM domains"))).scalar_one())


async def test_tampered_bundles_are_rejected_and_leave_nothing(
    engine: AsyncEngine,
    target: AsyncEngine,
    sessions: Sessions,
    store: StoreSettings,
    tmp_path: Path,
) -> None:
    runtime = StoreRuntime.from_settings(
        store.model_copy(update={"bundle": BundleSettings(compression="none")})
    )
    await populate(sessions, runtime)
    original = await exported_copy(engine, runtime, tmp_path, "original")

    # A changed byte: the file hash in the manifest no longer matches.
    flipped = tmp_path / "flipped"
    shutil.copytree(original, flipped)
    path = flipped / "nodes.jsonl"
    data = bytearray(path.read_bytes())
    data[10] = ord("0") if data[10] != ord("0") else ord("1")
    path.write_bytes(bytes(data))
    with pytest.raises(BundleError, match="sha256"):
        await import_bundle(target, flipped, runtime=runtime)

    # A forged object whose file hash is recomputed in the manifest: its own hash gives it away.
    forged = tmp_path / "forged"
    shutil.copytree(original, forged)
    lines = (forged / "objects.jsonl").read_text().splitlines()
    record = json.loads(lines[0])
    record["body"]["values"] = {"forged": "yes"}
    lines[0] = json.dumps(record)
    (forged / "objects.jsonl").write_text("\n".join(lines) + "\n")
    refresh_manifest(forged)
    with pytest.raises(BundleError, match=r"object .* does not match"):
        await import_bundle(target, forged, runtime=runtime)

    # A missing node: reachable content is incomplete.
    short = tmp_path / "short"
    shutil.copytree(original, short)
    lines = (short / "nodes.jsonl").read_text().splitlines()
    (short / "nodes.jsonl").write_text("\n".join(lines[:-1]) + "\n")
    refresh_manifest(short, counts={"nodes": len(lines) - 1})
    with pytest.raises(BundleError, match="lacks"):
        await import_bundle(target, short, runtime=runtime)

    # Wrong Domain id, unknown format version.
    with pytest.raises(BundleError, match="cannot be imported as"):
        await import_bundle(target, original, domain_id="other", runtime=runtime)
    future = tmp_path / "future"
    shutil.copytree(original, future)
    manifest = json.loads((future / "manifest.json").read_text())
    manifest["format_version"] = 99
    (future / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(BundleError, match="not supported"):
        await import_bundle(target, future, runtime=runtime)

    # Nothing was left behind by the failures, and the intact bundle still imports.
    assert await domain_rows(target) == 0
    await import_bundle(target, original, runtime=runtime)
    assert await domain_rows(target) == 1


def refresh_manifest(root: Path, counts: dict[str, int] | None = None) -> None:
    """Recompute the file hashes of a (tampered) bundle, as a forger would."""
    manifest = json.loads((root / "manifest.json").read_text())
    for name, entry in manifest["files"].items():
        data = (root / name).read_bytes()
        entry["sha256"] = hashlib.sha256(data).hexdigest()
        entry["bytes"] = len(data)
    manifest["counts"].update(counts or {})
    (root / "manifest.json").write_text(json.dumps(manifest))


async def test_overlong_line_fails_the_import_and_leaves_nothing(
    engine: AsyncEngine,
    target: AsyncEngine,
    sessions: Sessions,
    store: StoreSettings,
    tmp_path: Path,
) -> None:
    runtime = StoreRuntime.from_settings(
        store.model_copy(
            update={"bundle": BundleSettings(compression="none", max_line_bytes=50_000)}
        )
    )
    await populate(sessions, runtime)
    bundle = await exported_copy(engine, runtime, tmp_path, "long")
    lines = (bundle / "objects.jsonl").read_text().splitlines()
    record = json.loads(lines[0])
    record["padding"] = "x" * 60_000
    lines[0] = json.dumps(record)
    (bundle / "objects.jsonl").write_text("\n".join(lines) + "\n")
    refresh_manifest(bundle)
    with pytest.raises(BundleError, match=r"objects\.jsonl line 1 is longer than .*max_line_bytes"):
        await import_bundle(target, bundle, runtime=runtime)
    assert await domain_rows(target) == 0


async def test_export_of_unknown_domain_fails(engine: AsyncEngine, tmp_path: Path) -> None:
    with pytest.raises(DomainNotFoundError):
        await export_domain(engine, "nope", tmp_path / "x")


def test_command_line_export_and_import(
    database_url: str,
    target_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    async def seed() -> None:
        engine = create_engine(DatabaseSettings(), url=database_url, pooled=False)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            runtime = StoreRuntime.from_settings(StoreSettings())
            async with factory() as session:
                await create_domain(session, domain(), author=AUTHOR, runtime=runtime)
            await run(
                factory, runtime, Clock(), [part("P-1", "Bearing")], schema_changes=schema_changes()
            )
        finally:
            await engine.dispose()

    asyncio.run(seed())
    out = tmp_path / "out"
    monkeypatch.setenv("LOREBASE__DATABASE__DSN", database_url)
    assert bundle_main(["export", "--domain", DOMAIN, "--out", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["domain_id"] == DOMAIN
    monkeypatch.setenv("LOREBASE__DATABASE__DSN", target_url)
    assert bundle_main(["import", "--in", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["rebuilt_branches"] == ["main"]
    assert bundle_main(["import", "--in", str(out)]) == 1  # the Domain now has content
    assert "already has" in capsys.readouterr().err
