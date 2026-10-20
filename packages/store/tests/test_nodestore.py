"""Postgres node and object stores: equivalence with the in-memory store, batching, bulk paths."""

from __future__ import annotations

import hashlib
import random
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from lorebase_core.canonical import ObjectType, canonical_bytes, object_hash
from lorebase_core.tree import (
    InMemoryNodeStore,
    TreeChange,
    TreeParams,
    tree_apply,
    tree_entries,
    tree_get,
)
from lorebase_store.bulk import chunks, uses_copy
from lorebase_store.models import Domain, StoredObject, TreeNode
from lorebase_store.nodestore import NodeCache, NodeReadStats, NodeRepository, PgNodeStore
from lorebase_store.objectstore import (
    OBJECT_TYPE_CODES,
    OBJECT_TYPES_BY_CODE,
    PgObjectStore,
    encode_object,
)
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import WriteSettings

DOMAIN = "nodes"
PARAMS = TreeParams(leaf_size=8)
PARTITIONS = ("k:alpha", "k:beta")


def value(label: object) -> bytes:
    return hashlib.sha256(repr(label).encode()).digest()


# Unit tests ---------------------------------------------------------------------------------


def test_node_cache_evicts_least_recently_used_by_bytes() -> None:
    cache = NodeCache(max_bytes=10)
    cache.put("d", b"a", b"1234")
    cache.put("d", b"b", b"1234")
    assert cache.get("d", b"a") == b"1234"  # a becomes most recent
    cache.put("d", b"c", b"1234")  # 12 bytes > 10: evicts b
    assert cache.get("d", b"b") is None
    assert cache.get("d", b"a") == b"1234"
    assert cache.get("other", b"a") is None  # scoped by Domain
    assert cache.size_bytes == 8
    disabled = NodeCache(max_bytes=0)
    disabled.put("d", b"a", b"1")
    assert len(disabled) == 0


def test_encode_object_is_canonical() -> None:
    first = encode_object(ObjectType.RECORD, {"kind": "k", "key": "a", "values": {"x": None}})
    second = encode_object("record", {"values": {}, "key": "a", "kind": "k"})
    assert first == second
    assert first.body == {"kind": "k", "key": "a", "values": {}}
    assert first.hash == object_hash(ObjectType.RECORD, first.body)
    assert first.size == len(canonical_bytes(first.body))
    assert {OBJECT_TYPES_BY_CODE[c] for c in OBJECT_TYPE_CODES.values()} == set(ObjectType)


def test_chunks_and_copy_threshold() -> None:
    assert [list(c) for c in chunks([1, 2, 3, 4, 5], 2)] == [[1, 2], [3, 4], [5]]
    assert uses_copy(10, WriteSettings(copy_threshold_rows=10))
    assert not uses_copy(9, WriteSettings(copy_threshold_rows=10))
    assert not uses_copy(10**6, WriteSettings(copy_threshold_rows=0))
    with pytest.raises(ValueError, match="positive"):
        list(chunks([1], 0))


# Integration tests --------------------------------------------------------------------------


async def seed_domain(session: AsyncSession) -> None:
    await session.execute(
        insert(Domain).values(
            id=DOMAIN,
            display_name=DOMAIN,
            leaf_size=PARAMS.leaf_size,
            created_at=datetime(2026, 10, 20, tzinfo=UTC),
            created_by="usr_t",
        )
    )


def random_batch(rng: random.Random, keys: list[str], size: int, deletes: bool) -> list[TreeChange]:
    picked = rng.sample(keys, size)
    return [
        TreeChange(
            PARTITIONS[int(key.rsplit("-", 1)[1]) % len(PARTITIONS)],
            key,
            None if deletes and rng.random() < 0.3 else value((key, rng.random())),
        )
        for key in picked
    ]


async def apply_batch(
    session: AsyncSession,
    runtime: StoreRuntime,
    root: bytes | None,
    changes: list[TreeChange],
) -> tuple[bytes, NodeReadStats]:
    def run(sync: Session) -> tuple[bytes, dict[bytes, bytes], NodeReadStats]:
        store = PgNodeStore(sync, DOMAIN, cache=runtime.node_cache, prefetch_batch=50)
        store.prefetch_paths(root, [(c.partition, c.key) for c in changes])
        new_root = tree_apply(store, root, changes, PARAMS)
        return new_root, store.take_pending(), store.stats

    new_root, pending, stats = await session.run_sync(run)
    settings = runtime.settings
    await NodeRepository(
        session, DOMAIN, writes=settings.writes, cache=runtime.node_cache
    ).put_many(pending)
    return new_root, stats


@pytest.mark.integration
async def test_pg_node_store_matches_memory_store(
    engine: AsyncEngine, runtime: StoreRuntime
) -> None:
    rng = random.Random(7)
    keys = [f"key-{i}" for i in range(2000)]
    memory = InMemoryNodeStore()
    memory_root: bytes | None = None
    root: bytes | None = None
    sessions = async_sessionmaker(engine)
    async with sessions() as session, session.begin():
        await seed_domain(session)
    for round_number in range(5):
        changes = random_batch(rng, keys, 700, deletes=round_number >= 2)
        memory_root = tree_apply(memory, memory_root, changes, PARAMS)
        async with sessions() as session, session.begin():
            root, stats = await apply_batch(session, runtime, root, changes)
        assert root == memory_root
        if round_number == 1:
            # Updates and inserts over prefetched paths read no node individually.
            assert stats.misses == 0
            assert stats.queries <= 10

    # A cold store (no cache) reads the same content back.
    async with sessions() as session:

        def read(sync: Session) -> dict[str, dict[str, bytes]]:
            store = PgNodeStore(sync, DOMAIN)
            return {p: dict(tree_entries(store, root, p)) for p in PARTITIONS}

        stored = await session.run_sync(read)
        assert stored == {p: dict(tree_entries(memory, memory_root, p)) for p in PARTITIONS}

        def get_one(sync: Session) -> bytes | None:
            return tree_get(
                PgNodeStore(sync, DOMAIN), root, PARTITIONS[0], next(iter(stored[PARTITIONS[0]]))
            )

        assert await session.run_sync(get_one) is not None

        repository = NodeRepository(session, DOMAIN, writes=runtime.settings.writes, batch=7)
        tree = await repository.load_tree(root)
        for partition in PARTITIONS:
            scanned: dict[str, bytes] = {}
            async for entries in repository.scan(tree.partitions[partition].root):
                scanned.update(entries)
            assert scanned == stored[partition]

        node_count = await session.scalar(select(func.count()).select_from(TreeNode))
        assert node_count is not None
        assert node_count >= len(memory) // 2


@pytest.mark.integration
@pytest.mark.parametrize("copy_threshold_rows", [0, 1])
async def test_object_store_bulk_paths(engine: AsyncEngine, copy_threshold_rows: int) -> None:
    writes = WriteSettings(insert_batch_rows=3, copy_threshold_rows=copy_threshold_rows)
    objects = [
        encode_object(ObjectType.RECORD, {"kind": "k", "key": str(i), "values": {"n": str(i)}})
        for i in range(10)
    ]
    async with async_sessionmaker(engine)() as session, session.begin():
        await seed_domain(session)
        store = PgObjectStore(session, DOMAIN, writes=writes, batch=4)
        await store.put_many(objects[:6])
        await store.put_many(objects)  # the first six already exist: ignored
        found = await store.get_many([o.hash for o in objects] + [value("absent")])
        assert {h: (s.object_type, s.body) for h, s in found.items()} == {
            o.hash: (o.object_type, o.body) for o in objects
        }
        count = await session.scalar(select(func.count()).select_from(StoredObject))
        assert count == len(objects)
