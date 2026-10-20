"""Read routing: round-robin order, the freshness guard and fallbacks."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.revision import Author, Origin, Revision, RevisionStats
from lorebase_store import migrations, replicas, testing
from lorebase_store.db import create_engine
from lorebase_store.replicas import (
    ReadRoute,
    ReadSessionFactory,
    ReadSource,
    ReplicaSet,
    ReplicaState,
    ReplicaUnavailableError,
    RouteReason,
)
from lorebase_store.repositories import (
    DomainRepository,
    DomainRow,
    RefRepository,
    RevisionRepository,
    scope_to_domain,
)
from lorebase_store.settings import DatabaseSettings, StoreSettings

D = "repo"
NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
UNREACHABLE_DSN = "postgresql://nobody@127.0.0.1:1/none"

Sessions = async_sessionmaker[AsyncSession]


def settings_for(*dsns: str, **values: Any) -> DatabaseSettings:
    return DatabaseSettings(read_dsn=dsns, **values)  # type: ignore[arg-type]


def lazy_sessions() -> Sessions:
    """Sessions of an engine that is never connected (unit tests do not use the network)."""
    return async_sessionmaker(create_engine(DatabaseSettings()), expire_on_commit=False)


# Unit tests ---------------------------------------------------------------------------------


async def test_rotation_is_round_robin() -> None:
    pool = ReplicaSet.from_settings(settings_for("postgresql://u@h0/db", "postgresql://u@h1/db"))
    assert pool is not None
    try:
        orders = [[r.name for r in pool.rotation()] for _ in range(4)]
        assert orders == [
            ["replica-0", "replica-1"],
            ["replica-1", "replica-0"],
            ["replica-0", "replica-1"],
            ["replica-1", "replica-0"],
        ]
    finally:
        await pool.dispose()


async def test_single_replica_rotation() -> None:
    pool = ReplicaSet.from_settings(settings_for("postgresql://u@h0/db"))
    assert pool is not None
    try:
        assert [[r.name for r in pool.rotation()] for _ in range(2)] == [["replica-0"]] * 2
    finally:
        await pool.dispose()


async def test_no_replicas_configured() -> None:
    assert ReplicaSet.from_settings(DatabaseSettings()) is None


async def test_endpoints_never_contain_credentials() -> None:
    pool = ReplicaSet.from_settings(
        settings_for("postgresql://app:hunter2-long@db-r1:6432/lorebase", "postgresql://u@db-r2/x")
    )
    assert pool is not None
    try:
        assert [r.endpoint for r in pool.replicas] == ["db-r1:6432", "db-r2:5432"]
        assert "hunter2" not in repr([pool.last_states()])
    finally:
        await pool.dispose()


def test_replica_state_limits() -> None:
    def state(lag: float | None, reachable: bool = True) -> ReplicaState:
        return ReplicaState("replica-0", "h:5432", reachable=reachable, lag_ms=lag)

    assert state(100).usable(5000)
    assert not state(6000).usable(5000)
    assert not state(None).usable(5000)  # unknown lag cannot be trusted
    assert state(None).usable(0)  # the lag check is disabled
    assert state(10**9).usable(0)
    assert not state(0, reachable=False).usable(0)


async def test_without_replicas_reads_use_the_primary() -> None:
    seen: list[ReadRoute] = []
    factory = ReadSessionFactory(lazy_sessions(), None, on_route=seen.append)
    async with factory.open() as read:
        assert read.route == ReadRoute(ReadSource.PRIMARY, RouteReason.NO_REPLICAS)
    assert seen == [read.route]


async def test_replicas_can_be_bypassed() -> None:
    pool = ReplicaSet.from_settings(settings_for("postgresql://u@h0/db"))
    assert pool is not None
    try:
        factory = ReadSessionFactory(lazy_sessions(), pool)
        async with factory.open(prefer_replica=False) as read:
            assert read.route == ReadRoute(ReadSource.PRIMARY, RouteReason.NOT_PREFERRED)
    finally:
        await pool.dispose()


async def test_pinned_read_needs_a_domain() -> None:
    factory = ReadSessionFactory(lazy_sessions(), None)
    with pytest.raises(ValueError, match="domain_id"):
        async with factory.open(expected_head=b"\x00" * 32):
            pass


@pytest.fixture
def probe_result(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the replica probe, so that no replica is contacted."""
    result: dict[str, Any] = {"lag_ms": 0.0, "head": None}

    async def probe(
        replica: replicas.Replica, session: AsyncSession, domain_id: str | None, branch: str | None
    ) -> tuple[ReplicaState, bytes | None]:
        state = ReplicaState(
            replica.name,
            replica.endpoint,
            reachable=True,
            in_recovery=True,
            lag_ms=result["lag_ms"],
        )
        return state, result["head"]

    monkeypatch.setattr(ReadSessionFactory, "_probe", staticmethod(probe))
    return result


async def test_a_lagging_replica_serves_nothing(probe_result: dict[str, Any]) -> None:
    probe_result["lag_ms"] = 9000.0
    pool = ReplicaSet.from_settings(settings_for("postgresql://u@h0/db"))
    assert pool is not None
    try:
        factory = ReadSessionFactory(lazy_sessions(), pool, max_lag_ms=5000)
        async with factory.open() as read:
            assert read.route == ReadRoute(
                ReadSource.PRIMARY, RouteReason.LAGGING, "replica-0", 9000.0
            )
        strict = ReadSessionFactory(
            lazy_sessions(), pool, max_lag_ms=5000, fallback_to_primary=False
        )
        with pytest.raises(ReplicaUnavailableError) as raised:
            async with strict.open():
                pass
        assert raised.value.reason is RouteReason.LAGGING
        unchecked = ReadSessionFactory(lazy_sessions(), pool, max_lag_ms=0)
        async with unchecked.open() as read:
            assert read.route.source is ReadSource.REPLICA
    finally:
        await pool.dispose()


# Integration tests --------------------------------------------------------------------------


def h(label: str) -> bytes:
    return hashlib.sha256(label.encode()).digest()


def revision(parents: tuple[bytes, ...], label: str) -> Revision:
    return Revision(
        parents=parents,
        root=h(f"root-{label}"),
        domain=D,
        author=Author(principal="usr_t"),
        message=label,
        origin=Origin(type="test"),
        created_at=NOW,
        stats=RevisionStats(partitions_changed=0, changes=0),
    )


async def seed(sessions: Sessions) -> tuple[bytes, bytes, bytes]:
    """Domain `repo`; revisions root <- a <- b; `main` points at `a`."""
    async with sessions() as session, session.begin():
        await DomainRepository(session).create(
            DomainRow(
                id=D,
                display_name="Repo",
                description=None,
                default_branch="main",
                leaf_size=512,
                settings={},
                created_at=NOW,
                created_by="usr_t",
            )
        )
        await scope_to_domain(session, D)
        revisions = RevisionRepository(session, D)
        root = await revisions.insert(revision((), "root"))
        a = await revisions.insert(revision((root.id,), "a"))
        b = await revisions.insert(revision((a.id,), "b"))
        await RefRepository(session, D).create("main", a.id, created_at=NOW, created_by="usr_t")
    return root.id, a.id, b.id


async def move_main(sessions: Sessions, expected: bytes, new: bytes, version: int) -> None:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, D)
        await RefRepository(session, D).compare_and_swap(
            "main",
            expected_head=expected,
            expected_version=version,
            new_head=new,
            actor="usr_t",
            at=NOW,
            reason="commit",
        )


@pytest.fixture
async def primary(engine: AsyncEngine) -> Sessions:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def lagging_replica_url(
    postgres_server_url: str, database: DatabaseSettings, store: StoreSettings
) -> Iterator[str]:
    """A second database standing in for a replica that has not replayed the latest commit."""
    with testing.scratch_database(postgres_server_url) as url:
        migrations.upgrade(database=database, store=store, url=url)
        yield url


@pytest.fixture
async def replica_sessions(lagging_replica_url: str) -> AsyncIterator[Sessions]:
    engine = create_engine(DatabaseSettings(), url=lagging_replica_url, pooled=False)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def factory_for(primary: Sessions, *urls: str, **values: Any) -> ReadSessionFactory:
    return ReadSessionFactory.from_settings(primary, settings_for(*urls, **values))


@pytest.mark.integration
async def test_replica_holding_the_expected_head_serves_the_read(
    database_url: str, primary: Sessions
) -> None:
    _, a, _ = await seed(primary)
    factory = factory_for(primary, database_url)
    try:
        async with factory.open(domain_id=D, branch="main", expected_head=a) as read:
            assert read.route.source is ReadSource.REPLICA
            assert read.route.reason is RouteReason.REPLICA_FRESH
            assert read.route.replica == "replica-0"
            assert (await RefRepository(read.session, D).require("main")).head == a
        # The branch defaults to the Domain's default branch.
        async with factory.open(domain_id=D, expected_head=a) as read:
            assert read.route.source is ReadSource.REPLICA
    finally:
        await factory.dispose()


@pytest.mark.integration
async def test_other_head_unknown_branch_or_domain_is_stale(
    database_url: str, primary: Sessions
) -> None:
    root, a, _ = await seed(primary)
    factory = factory_for(primary, database_url)
    try:
        for domain, branch, head in [(D, "main", root), (D, "missing", a), ("nope", None, a)]:
            async with factory.open(domain_id=domain, branch=branch, expected_head=head) as read:
                assert read.route.source is ReadSource.PRIMARY
                assert read.route.reason is RouteReason.STALE
    finally:
        await factory.dispose()


@pytest.mark.integration
async def test_stale_replica_falls_back_then_serves_once_caught_up(
    database_url: str,
    lagging_replica_url: str,
    primary: Sessions,
    replica_sessions: Sessions,
) -> None:
    _, a, b = await seed(primary)
    await seed(replica_sessions)
    await move_main(primary, a, b, 1)  # the primary is ahead of the replica
    factory = factory_for(primary, lagging_replica_url)
    try:
        async with factory.open(domain_id=D, branch="main", expected_head=b) as read:
            assert read.route.source is ReadSource.PRIMARY
            assert read.route.reason is RouteReason.STALE
            assert (await RefRepository(read.session, D).require("main")).head == b
        # Without a pin the replica is good enough (eventual consistency).
        async with factory.open(domain_id=D, branch="main") as read:
            assert read.route.source is ReadSource.REPLICA
            assert (await RefRepository(read.session, D).require("main")).head == a
        await move_main(replica_sessions, a, b, 1)  # the replica replays the commit
        async with factory.open(domain_id=D, branch="main", expected_head=b) as read:
            assert read.route.source is ReadSource.REPLICA
    finally:
        await factory.dispose()


@pytest.mark.integration
async def test_read_your_writes_reads_the_head_from_the_primary(
    lagging_replica_url: str,
    primary: Sessions,
    replica_sessions: Sessions,
) -> None:
    _, a, b = await seed(primary)
    await seed(replica_sessions)
    await move_main(primary, a, b, 1)
    factory = factory_for(primary, lagging_replica_url)
    try:
        async with factory.open(domain_id=D, read_your_writes=True) as read:
            assert read.route.source is ReadSource.PRIMARY
            assert read.route.reason is RouteReason.STALE
        await move_main(replica_sessions, a, b, 1)
        async with factory.open(domain_id=D, read_your_writes=True) as read:
            assert read.route.source is ReadSource.REPLICA
    finally:
        await factory.dispose()


@pytest.mark.integration
async def test_stale_replica_without_fallback_is_unavailable(
    lagging_replica_url: str,
    primary: Sessions,
    replica_sessions: Sessions,
) -> None:
    _, a, b = await seed(primary)
    await seed(replica_sessions)
    await move_main(primary, a, b, 1)
    factory = factory_for(primary, lagging_replica_url, read_fallback_to_primary=False)
    try:
        with pytest.raises(ReplicaUnavailableError) as raised:
            async with factory.open(domain_id=D, expected_head=b):
                pass
        assert raised.value.reason is RouteReason.STALE
        async with factory.open(domain_id=D, expected_head=a) as read:  # still fine when fresh
            assert read.route.source is ReadSource.REPLICA
    finally:
        await factory.dispose()


@pytest.mark.integration
async def test_unreachable_replica_is_skipped(database_url: str, primary: Sessions) -> None:
    _, a, _ = await seed(primary)
    factory = factory_for(primary, UNREACHABLE_DSN, database_url)
    try:
        for _ in range(2):  # whichever replica round-robin starts with, the live one serves
            async with factory.open(domain_id=D, expected_head=a) as read:
                assert read.route.replica == "replica-1"
        states = {s.name: s for s in (factory.replicas.last_states() if factory.replicas else [])}
        assert not states["replica-0"].reachable
        assert states["replica-0"].error
        assert states["replica-1"].reachable
    finally:
        await factory.dispose()


@pytest.mark.integration
async def test_only_unreachable_replicas_fall_back_or_fail(primary: Sessions) -> None:
    factory = factory_for(primary, UNREACHABLE_DSN)
    strict = factory_for(primary, UNREACHABLE_DSN, read_fallback_to_primary=False)
    try:
        async with factory.open() as read:
            assert read.route.source is ReadSource.PRIMARY
            assert read.route.reason is RouteReason.UNREACHABLE
            assert (await read.session.execute(text("SELECT 1"))).scalar_one() == 1
        with pytest.raises(ReplicaUnavailableError):
            async with strict.open():
                pass
    finally:
        await factory.dispose()
        await strict.dispose()


@pytest.mark.integration
async def test_probe_reports_replication_state(database_url: str, primary: Sessions) -> None:
    factory = factory_for(primary, database_url, UNREACHABLE_DSN)
    try:
        assert factory.replicas is not None
        good, bad = await factory.replicas.probe(timeout_s=2)
        assert good.reachable
        assert good.in_recovery is False  # the test server is no standby
        assert good.lag_ms == 0
        assert good.replay_lsn is None
        assert good.usable(5000)
        assert not bad.reachable
        assert bad.lag_ms is None
        assert factory.replicas.last_states() == [good, bad]
    finally:
        await factory.dispose()
