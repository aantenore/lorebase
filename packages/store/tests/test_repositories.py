"""Store repositories against Postgres, plus their pure helpers."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from lorebase_core.model import (
    Domain,
    ImportOrigin,
    Link,
    ProvenanceEntry,
    Record,
    RecordIdentity,
    Schema,
)
from lorebase_core.model.record import LinkIdentity
from lorebase_core.revision import (
    Author,
    ChangesetEntry,
    Origin,
    Revision,
    RevisionStats,
    is_ancestor,
)
from lorebase_core.validate import LinkUpsert, RecordDelete, RecordUpsert
from lorebase_store.models import AuditEntry as AuditRow
from lorebase_store.models import OutboxEvent
from lorebase_store.repositories import (
    AuditEntry,
    AuditRepository,
    ChangesetRepository,
    DomainNotFoundError,
    DomainRepository,
    DomainRow,
    HistoryRow,
    LinkRow,
    LockTimeoutError,
    MaterializedIndexView,
    MaterializedRepository,
    OutboxRepository,
    PgRevisionGraph,
    ProvenanceRepository,
    ProvenanceRow,
    RecordRow,
    RefMovedError,
    RefRepository,
    RevisionRepository,
    SchemaRow,
    advisory_lock_key,
    chain_hash,
    scope_to_domain,
)
from lorebase_store.repositories.refs import sqlstate
from lorebase_store.settings import StoreSettings

D = "repo"
NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
KIND = "knd_01JB8Z00000000000000000001"
RELATION = "rel_01JB8Z00000000000000000001"
ATTR = "att_01JB8Z00000000000000000001"


def h(label: str) -> bytes:
    return hashlib.sha256(label.encode()).digest()


# Pure helpers -------------------------------------------------------------------------------


def test_advisory_lock_key_is_stable_and_scoped() -> None:
    key = advisory_lock_key("supply", "main")
    assert key == advisory_lock_key("supply", "main")
    assert -(2**63) <= key < 2**63
    assert key != advisory_lock_key("supply", "main2")
    assert advisory_lock_key("ab", "c") != advisory_lock_key("a", "bc")


def test_chain_hash_links_entries() -> None:
    entry = AuditEntry(domain_id="d", at=NOW, actor="usr", action="a", details={"x": 1})
    first = chain_hash(None, entry)
    assert first == chain_hash(None, entry)
    assert chain_hash(first, entry) != first


def test_sqlstate_follows_wrapped_errors() -> None:
    class DriverError(Exception):
        sqlstate = "55P03"

    wrapper = RuntimeError("wrapped")
    wrapper.__cause__ = DriverError()
    assert sqlstate(wrapper) == "55P03"
    assert sqlstate(RuntimeError()) is None


# Fixtures -----------------------------------------------------------------------------------


@pytest.fixture
async def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
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
    return factory


def revision(parents: tuple[bytes, ...], label: str, offset: int = 0) -> Revision:
    return Revision(
        parents=parents,
        root=h(f"root-{label}"),
        domain=D,
        author=Author(principal="usr_t"),
        message=label,
        origin=Origin(type="test"),
        created_at=NOW + timedelta(seconds=offset),
        stats=RevisionStats(partitions_changed=0, changes=0),
    )


async def seed_revisions(session: AsyncSession) -> list[bytes]:
    """root <- a <- b, and c with parents (b, a)."""
    repository = RevisionRepository(session, D)
    root = await repository.insert(revision((), "root"))
    a = await repository.insert(revision((root.id,), "a", 1))
    b = await repository.insert(revision((a.id,), "b", 2))
    c = await repository.insert(revision((b.id, a.id), "c", 3))
    return [root.id, a.id, b.id, c.id]


# Integration tests --------------------------------------------------------------------------


@pytest.mark.integration
async def test_domains(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        domains = DomainRepository(session)
        found = await domains.require(D)
        assert found.leaf_size == 512
        assert await domains.get("missing") is None
        with pytest.raises(DomainNotFoundError):
            await domains.require("missing")


@pytest.mark.integration
async def test_revisions_parents_generation_and_graph(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, D)
        root, a, b, c = await seed_revisions(session)
        repository = RevisionRepository(session, D)
        stored = await repository.require(c)
        assert stored.parents == (b, a)
        assert stored.generation == 4
        assert await repository.generations([root, b]) == {root: 1, b: 3}
        assert await repository.first_parent_chain(c) == [c, b, a, root]

        def ancestry(sync: Session) -> tuple[bool, bool]:
            graph = PgRevisionGraph(sync, D)
            return is_ancestor(graph, root, c), is_ancestor(graph, c, a)

        assert await session.run_sync(ancestry) == (True, False)


@pytest.mark.integration
async def test_refs_compare_and_swap_and_log(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session, session.begin():
        root, a, b, _ = await seed_revisions(session)
        refs = RefRepository(session, D)
        created = await refs.create("main", root, created_at=NOW, created_by="usr_t")
        assert created.version == 1
        version = await refs.compare_and_swap(
            "main",
            expected_head=root,
            expected_version=1,
            new_head=a,
            actor="usr_t",
            at=NOW,
            reason="commit",
        )
        assert version == 2
        with pytest.raises(RefMovedError):
            await refs.compare_and_swap(
                "main",
                expected_head=root,
                expected_version=1,
                new_head=b,
                actor="usr_t",
                at=NOW,
                reason="commit",
            )
        current = await refs.require("main")
        assert (current.head, current.version) == (a, 2)
        log = await refs.log("main")
        assert [(e.seq, e.old_head, e.new_head) for e in log] == [(1, None, root), (2, root, a)]
        assert [r.name for r in await refs.all()] == ["main"]


@pytest.mark.integration
async def test_concurrent_compare_and_swap_has_one_winner(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session, session.begin():
        root, a, b, _ = await seed_revisions(session)
        await RefRepository(session, D).create("main", root, created_at=NOW, created_by="usr_t")

    async def swap(target: bytes) -> bool:
        async with sessions() as session, session.begin():
            try:
                await RefRepository(session, D).compare_and_swap(
                    "main",
                    expected_head=root,
                    expected_version=1,
                    new_head=target,
                    actor="usr_t",
                    at=NOW,
                    reason="race",
                )
            except RefMovedError:
                return False
            return True

    outcomes = await asyncio.gather(swap(a), swap(b))
    assert sorted(outcomes) == [False, True]


@pytest.mark.integration
async def test_branch_lock_times_out(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as holder, holder.begin():
        await RefRepository(holder, D).lock("main", timeout_ms=1000)
        async with sessions() as waiter:
            await waiter.begin()
            with pytest.raises(LockTimeoutError):
                await RefRepository(waiter, D).lock("main", timeout_ms=100)
            await waiter.rollback()
        # Other branches are not blocked; the previous lock_timeout is restored.
        async with sessions() as other, other.begin():
            await RefRepository(other, D).lock("dev", timeout_ms=100)
            assert await other.scalar(text("SHOW lock_timeout")) == "0"


@pytest.mark.integration
async def test_changesets_provenance_and_events(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    settings = StoreSettings()
    async with sessions() as session, session.begin():
        _, a, _, _ = await seed_revisions(session)
        entries = [
            ChangesetEntry("k:x", "1", None, h("1"), {ATTR: "prv_1"}),
            ChangesetEntry("k:x", "2", h("2"), None),
        ]
        changesets = ChangesetRepository(session, D, writes=settings.writes)
        await changesets.insert(a, entries)
        assert await changesets.entries(a) == entries
        assert (await changesets.entries(a))[0].provenance == {ATTR: "prv_1"}

        entry = ProvenanceEntry(
            id="prv_01JB8Z00000000000000000001",
            origin=ImportOrigin(batch_id="b1"),
            actor="usr_t",
            at=NOW,
        )
        provenance = ProvenanceRepository(session, D, writes=settings.writes)
        await provenance.insert_many([ProvenanceRow.from_entry(entry)] * 2)
        found = await provenance.get_many([entry.id, "prv_missing"])
        assert list(found) == [entry.id]
        assert found[entry.id].origin_ref == {"type": "import", "batch_id": "b1"}
        assert found[entry.id].at == NOW

        audit = AuditRepository(session)
        first = await audit.append(AuditEntry(domain_id=D, at=NOW, actor="usr_t", action="x"))
        second = await audit.append(AuditEntry(domain_id=D, at=NOW, actor="usr_t", action="y"))
        rows = (await session.execute(select(AuditRow).order_by(AuditRow.seq))).scalars().all()
        assert [(r.prev_hash, r.entry_hash) for r in rows] == [(None, first), (first, second)]
        event = await OutboxRepository(session).enqueue(D, "test.event", {"n": 1})
        stored = await session.get(OutboxEvent, event)
        assert stored is not None
        assert stored.payload == {"n": 1}


@pytest.mark.integration
async def test_materialized_rows_and_index_view(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    settings = StoreSettings()
    async with sessions() as session, session.begin():
        root, a, _, _ = await seed_revisions(session)
        refs = RefRepository(session, D)
        await refs.create("main", root, created_at=NOW, created_by="usr_t")
        await refs.create("copy", root, created_at=NOW, created_by="usr_t")
        rows = MaterializedRepository(session, D, "main", writes=settings.writes)
        await rows.set_schema(SchemaRow(h("schema"), {"v": 1}))
        await rows.set_schema(SchemaRow(h("schema2"), {"v": 2}))
        assert await rows.schema() == SchemaRow(h("schema2"), {"v": 2})
        records = [
            RecordRow(KIND, key, {ATTR: f"v{key}"}, f"Title {key}", h(key), {}, a, NOW)
            for key in ("1", "2", "3")
        ]
        await rows.upsert_records(records)
        await rows.upsert_records([RecordRow(KIND, "1", {ATTR: "new"}, None, h("1b"), {}, a, NOW)])
        await rows.delete_records(KIND, ["3"])
        found = await rows.records(KIND, ["1", "2", "3"])
        assert sorted(found) == ["1", "2"]
        assert found["1"].values == {ATTR: "new"}
        await rows.upsert_links(
            [
                LinkRow(RELATION, "1", "2", {}, h("l12"), {}),
                LinkRow(RELATION, "2", "1", {}, h("l21"), {}),
            ]
        )
        await rows.delete_links(RELATION, [("2", "1")])
        assert list(await rows.links(RELATION, [("1", "2"), ("2", "1")])) == [("1", "2")]
        await rows.insert_history(
            [HistoryRow("k:" + KIND, "1", a, NOW, None, h("1"), {ATTR: "prv_1"})]
        )
        await rows.refresh_search(KIND, [(ATTR, "B")], config="simple")
        search = await session.scalar(
            text("SELECT search::text FROM mat_records WHERE key = '2' AND branch = 'main'")
        )
        assert search is not None
        assert {"'title':1A", "'2':2A", "'v2':3B"} <= set(search.split())
        assert await rows.record_counts() == {KIND: 2}
        assert await rows.link_counts() == {RELATION: 1}

        copy = MaterializedRepository(session, D, "copy", writes=settings.writes)
        await copy.copy_from("main")
        assert await copy.record_counts() == {KIND: 2}
        assert (await copy.history("k:" + KIND, "1"))[0].provenance == {ATTR: "prv_1"}
        await copy.clear()
        assert await copy.record_counts() == {}

        def validate(sync: Session) -> tuple[bool, bool, int, list[tuple[str, str]], int]:
            view = MaterializedIndexView(sync, D, "main", batch=100)
            view.prefetch_for(
                _empty_schema(),
                [
                    RecordUpsert(record=Record(kind=KIND, key="1", values={})),
                    RecordDelete(kind=KIND, key="2"),
                    LinkUpsert(link=Link(relation=RELATION, from_key="1", to_key="2")),
                ],
            )
            view.prefetch_records([RecordIdentity(KIND, "9")])
            view.prefetch_links([LinkIdentity(RELATION, "1", "2")])
            before = view.stats.queries
            answers = (
                view.record_exists(KIND, "1"),
                view.record_exists(KIND, "9"),
                before - view.stats.queries,
            )
            pairs = list(view.links_of(RELATION, from_key="1"))
            return (*answers, pairs, view.count_links(RELATION, to_key="2"))

        exists, missing, extra_queries, pairs, count = await session.run_sync(validate)
        assert (exists, missing, extra_queries) == (True, False, 0)
        assert pairs == [("1", "2")]
        assert count == 1


def _empty_schema() -> Schema:
    return Schema(domain=Domain(id=D))
