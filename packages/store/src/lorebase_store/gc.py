"""Garbage collection of one Domain: mark and sweep (design 6.13).

Marking. Roots are the heads of all refs (branches and tags), the Revisions and merge attempts
of open Proposals, and every Revision younger than `gc.grace_seconds`. Ancestors of a root are
reachable; so are the tree nodes of every reachable Revision root and the objects at the leaves
of those nodes. Marks live in temporary tables of the collecting connection, so memory stays
bounded by one tree level while shared subtrees are visited once (a node that is already marked
is not descended into).

Sweeping. Unmarked Revisions (with their parents, changesets and history rows), orphaned
provenance entries, tree nodes and objects are deleted in batches, newest Revisions first so a
child never outlives its parent. Before each batch of deletions the sweep takes, without
waiting, the advisory locks of every ref of the Domain (a commit holds its branch lock until
it ends) and marks again from the current refs: a commit that landed after the first marking is
protected, and a commit still in flight makes the batch wait. The grace period covers commits
that are visible but are not (or no longer) reachable from a ref.

Assumption: a commit writes its objects, nodes and Revision in one transaction (design 6.6), so
a stored object or node is never ahead of the Revision that reaches it. Rows written by other
means before their Revision is stored are not protected by the grace period.

One collection per Domain runs at a time (a session advisory lock distinct from the branch
locks); a second one fails with `GcBusyError`.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from sqlalchemy import ARRAY, BigInteger, LargeBinary, bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from lorebase_core.tree import BranchNode, LeafNode, NodeType, RootNode, decode_node
from lorebase_store.bulk import chunks
from lorebase_store.jobs import JobRow
from lorebase_store.repositories.domains import DomainNotFoundError
from lorebase_store.repositories.refs import advisory_lock_key
from lorebase_store.repositories.scope import DOMAIN_IDS_SETTING
from lorebase_store.runtime import StoreRuntime, default_runtime
from lorebase_store.settings import GcSettings

log = logging.getLogger(__name__)

Executor = AsyncSession | AsyncConnection
"""Anything that can run statements; marking works inside a session transaction or on a
dedicated connection."""

Step = Callable[[], AbstractAsyncContextManager[None]]
"""Opens the transaction of one unit of work (or does nothing inside an open transaction)."""

PhaseHook = Callable[[str], Awaitable[None]]
"""Called between phases, outside any transaction; for progress reporting and tests."""

_LOCK_SCOPE = b"lb1\x00gc-lock\x00"
_ROOT_NODE_TYPE = int(NodeType.ROOT)
_NODE_TYPE_OFFSET = 1
_OBJECT_MARK_CHUNK = 20000

PHASE_MARKED = "marked"
PHASE_REVISIONS_SWEPT = "revisions-swept"
PHASE_NODES_SWEPT = "nodes-swept"
PHASE_OBJECTS_SWEPT = "objects-swept"


class GcError(Exception):
    """A collection could not run to completion."""


class GcBusyError(GcError):
    """Another collection of the same Domain is running."""


class _BranchBusyError(Exception):
    """A branch lock is held by a commit in flight."""


def gc_lock_key(domain_id: str) -> int:
    """Signed 64-bit advisory lock key of a Domain's collection (distinct from branch locks)."""
    digest = hashlib.sha256(_LOCK_SCOPE + domain_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _bytea_array(name: str) -> Any:
    return bindparam(name, type_=ARRAY(LargeBinary()))


# --------------------------------------------------------------------------------------
# Temporary mark tables


_MARK_TABLES = (
    "lb_gc_roots",
    "lb_gc_revs",
    "lb_gc_nodes",
    "lb_gc_objects",
    "lb_gc_dead",
    "lb_gc_prov",
)

_CREATE_MARK_TABLES = (
    "CREATE TEMP TABLE IF NOT EXISTS lb_gc_roots (id bytea PRIMARY KEY)",
    "CREATE TEMP TABLE IF NOT EXISTS lb_gc_revs (id bytea PRIMARY KEY, root bytea NOT NULL)",
    "CREATE TEMP TABLE IF NOT EXISTS lb_gc_nodes (hash bytea PRIMARY KEY)",
    "CREATE TEMP TABLE IF NOT EXISTS lb_gc_objects (hash bytea PRIMARY KEY)",
    "CREATE TEMP TABLE IF NOT EXISTS lb_gc_dead (id bytea PRIMARY KEY, generation bigint NOT NULL)",
    "CREATE INDEX IF NOT EXISTS lb_gc_dead_order ON lb_gc_dead (generation DESC, id DESC)",
    "CREATE TEMP TABLE IF NOT EXISTS lb_gc_prov (id text PRIMARY KEY)",
)

_ROOTS_REFS = text(
    "INSERT INTO lb_gc_roots (id) SELECT DISTINCT head FROM refs WHERE domain_id = :d "
    "ON CONFLICT DO NOTHING"
)
_ROOTS_YOUNG = text(
    "INSERT INTO lb_gc_roots (id) SELECT id FROM revisions "
    "WHERE domain_id = :d AND created_at >= :cutoff ON CONFLICT DO NOTHING"
)
_ATTEMPT_HASHES = """
    SELECT unnest(ARRAY[m.base, m.ours, m.theirs]) AS h
    FROM merge_attempts m
    JOIN proposals p ON p.domain_id = m.domain_id AND p.id = m.proposal_id
    WHERE m.domain_id = :d AND p.status = 'open' AND m.status NOT IN ('merged', 'abandoned')
"""
_ROOTS_ATTEMPTS = text(
    f"INSERT INTO lb_gc_roots (id) SELECT DISTINCT h FROM ({_ATTEMPT_HASHES}) a "
    "ON CONFLICT DO NOTHING"
)
_ATTEMPT_TREES = text(
    f"""
    SELECT DISTINCT n.hash FROM ({_ATTEMPT_HASHES}) a
    JOIN tree_nodes n ON n.domain_id = :d AND n.hash = a.h
    WHERE get_byte(n.body, {_NODE_TYPE_OFFSET}) = {_ROOT_NODE_TYPE}
    """
)
_WALK = text(
    """
    WITH RECURSIVE walk(id) AS (
        SELECT r.id FROM lb_gc_roots r
        WHERE NOT EXISTS (SELECT 1 FROM lb_gc_revs m WHERE m.id = r.id)
        UNION
        SELECT p.parent_id FROM walk
        JOIN revision_parents p ON p.domain_id = :d AND p.revision_id = walk.id
        WHERE NOT EXISTS (SELECT 1 FROM lb_gc_revs m WHERE m.id = p.parent_id)
    )
    INSERT INTO lb_gc_revs (id, root)
    SELECT rev.id, rev.root FROM walk
    JOIN revisions rev ON rev.domain_id = :d AND rev.id = walk.id
    ON CONFLICT DO NOTHING
    RETURNING root
    """
)
_MARK_NODES = text(
    "INSERT INTO lb_gc_nodes (hash) SELECT h FROM unnest(CAST(:hs AS bytea[])) AS t(h) "
    "ON CONFLICT DO NOTHING RETURNING hash"
).bindparams(_bytea_array("hs"))
_MARK_OBJECTS = text(
    "INSERT INTO lb_gc_objects (hash) SELECT h FROM unnest(CAST(:hs AS bytea[])) AS t(h) "
    "ON CONFLICT DO NOTHING"
).bindparams(_bytea_array("hs"))
_READ_NODES = text(
    "SELECT hash, body FROM tree_nodes WHERE domain_id = :d AND hash = ANY(CAST(:hs AS bytea[]))"
).bindparams(_bytea_array("hs"))


@dataclass(slots=True)
class MarkStats:
    """What marking reached."""

    revisions: int = 0
    nodes: int = 0
    objects: int = 0
    missing_nodes: int = 0


@asynccontextmanager
async def _open_transaction() -> AsyncIterator[None]:
    yield


class Marker:
    """Marks the Revisions, tree nodes and objects reachable from a set of roots.

    Marks accumulate in temporary tables (`lb_gc_revs`, `lb_gc_nodes`, `lb_gc_objects`) of the
    executor's connection. `step` brackets each unit of work; pass a function that opens a
    transaction when the executor has none open, or one that does nothing inside a transaction
    the caller manages.
    """

    def __init__(
        self,
        executor: Executor,
        domain_id: str,
        *,
        batch: int,
        step: Step = _open_transaction,
    ) -> None:
        self._ex = executor
        self._domain = domain_id
        self._batch = batch
        self._step = step
        self.missing_nodes = 0

    async def prepare(self) -> None:
        """Create the mark tables (if needed) and empty them."""
        async with self._step():
            for statement in _CREATE_MARK_TABLES:
                await self._ex.execute(text(statement))
            await self._ex.execute(text("TRUNCATE " + ", ".join(_MARK_TABLES)))

    async def drop(self) -> None:
        async with self._step():
            await self._ex.execute(text("DROP TABLE IF EXISTS " + ", ".join(_MARK_TABLES)))

    async def mark(self, *, cutoff: datetime | None = None, attempts: bool = True) -> None:
        """Mark from the current ref heads, from the retained merge attempts when `attempts`,
        and from Revisions created at or after `cutoff` when given.

        Already marked Revisions and nodes are not visited again, so calling this again marks
        only what became reachable since.
        """
        async with self._step():
            domain = {"d": self._domain}
            await self._ex.execute(text("DELETE FROM lb_gc_roots"))
            await self._ex.execute(_ROOTS_REFS, domain)
            if attempts:
                await self._ex.execute(_ROOTS_ATTEMPTS, domain)
            if cutoff is not None:
                await self._ex.execute(_ROOTS_YOUNG, {**domain, "cutoff": cutoff})
            new_roots = list((await self._ex.execute(_WALK, domain)).scalars())
            if attempts:
                new_roots.extend((await self._ex.execute(_ATTEMPT_TREES, domain)).scalars())
        await self._mark_trees(list(dict.fromkeys(new_roots)))

    async def stats(self) -> MarkStats:
        async with self._step():
            counts = [
                int((await self._ex.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())
                for table in ("lb_gc_revs", "lb_gc_nodes", "lb_gc_objects")
            ]
        return MarkStats(counts[0], counts[1], counts[2], self.missing_nodes)

    async def _mark_trees(self, roots: list[bytes]) -> None:
        """Mark the nodes below `roots` level by level, in batched reads."""
        frontier = roots
        while frontier:
            following: list[bytes] = []
            for chunk in chunks(frontier, self._batch):
                following.extend(await self._mark_nodes(list(chunk)))
            frontier = list(dict.fromkeys(following))

    async def _mark_nodes(self, hashes: list[bytes]) -> list[bytes]:
        async with self._step():
            fresh = list((await self._ex.execute(_MARK_NODES, {"hs": hashes})).scalars())
            if not fresh:
                return []
            rows = (await self._ex.execute(_READ_NODES, {"d": self._domain, "hs": fresh})).all()
            self.missing_nodes += len(fresh) - len(rows)
            children: list[bytes] = []
            objects: list[bytes] = []
            for _, body in rows:
                node = decode_node(bytes(body))
                if isinstance(node, LeafNode):
                    objects.extend(digest for _, digest in node.entries)
                elif isinstance(node, BranchNode):
                    children.extend(child for _, child in node.children)
                elif isinstance(node, RootNode):
                    children.extend(ref.root for _, ref in node.partitions)
            for chunk in chunks(objects, _OBJECT_MARK_CHUNK):
                await self._ex.execute(_MARK_OBJECTS, {"hs": list(chunk)})
            return children


# --------------------------------------------------------------------------------------
# Report


@dataclass(slots=True)
class GcReport:
    """Outcome of a collection. With `dry_run`, the counts are what a collection would delete."""

    domain_id: str
    dry_run: bool
    cutoff: str
    marked_revisions: int = 0
    marked_nodes: int = 0
    marked_objects: int = 0
    missing_nodes: int = 0
    revisions: int = 0
    changesets: int = 0
    history_rows: int = 0
    provenance: int = 0
    nodes: int = 0
    node_bytes: int = 0
    objects: int = 0
    object_bytes: int = 0
    mark_seconds: float = 0.0
    sweep_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------------------
# Sweep SQL

_DEAD_FILL = text(
    """
    INSERT INTO lb_gc_dead (id, generation)
    SELECT r.id, r.generation FROM revisions r
    WHERE r.domain_id = :d AND NOT EXISTS (SELECT 1 FROM lb_gc_revs m WHERE m.id = r.id)
    """
)
_DEAD_FIRST = text(
    "SELECT id, generation FROM lb_gc_dead ORDER BY generation DESC, id DESC LIMIT :n"
)
_DEAD_NEXT = text(
    "SELECT id, generation FROM lb_gc_dead WHERE (generation, id) < (:g, :i) "
    "ORDER BY generation DESC, id DESC LIMIT :n"
)
_DEAD_STILL = text(
    "SELECT t.id FROM unnest(CAST(:ids AS bytea[])) AS t(id) "
    "WHERE NOT EXISTS (SELECT 1 FROM lb_gc_revs m WHERE m.id = t.id)"
).bindparams(_bytea_array("ids"))
_PAIRS = "CASE WHEN jsonb_typeof(c.provenance) = 'object' THEN c.provenance ELSE '{}'::jsonb END"
"""The provenance map of a changeset row `c`, empty when the column is NULL or a JSON null."""
_PROV_COLLECT = text(
    f"""
    INSERT INTO lb_gc_prov (id)
    SELECT e.value FROM changesets c, LATERAL jsonb_each_text({_PAIRS}) e
    WHERE c.domain_id = :d AND c.revision_id = ANY(CAST(:ids AS bytea[]))
    ON CONFLICT DO NOTHING
    """
).bindparams(_bytea_array("ids"))
_HISTORY_DELETE = text(
    "DELETE FROM mat_history WHERE domain_id = :d AND revision_id = ANY(CAST(:ids AS bytea[]))"
).bindparams(_bytea_array("ids"))
_PARENTS_DELETE = text(
    "DELETE FROM revision_parents WHERE domain_id = :d AND revision_id = ANY(CAST(:ids AS bytea[]))"
).bindparams(_bytea_array("ids"))
_CHANGESETS_DELETE = text(
    "DELETE FROM changesets WHERE domain_id = :d AND revision_id = ANY(CAST(:ids AS bytea[]))"
).bindparams(_bytea_array("ids"))
_REVISIONS_DELETE = text(
    "DELETE FROM revisions WHERE domain_id = :d AND id = ANY(CAST(:ids AS bytea[]))"
).bindparams(_bytea_array("ids"))
_PROV_USED = (
    f"SELECT e.value FROM changesets c, LATERAL jsonb_each_text({_PAIRS}) e WHERE c.domain_id = :d"
)
_PROV_DELETE = text(
    f"DELETE FROM provenance p WHERE p.domain_id = :d AND p.id IN "
    f"(SELECT id FROM lb_gc_prov EXCEPT {_PROV_USED})"
)
_PROV_DRY = text(
    f"""
    SELECT count(*) FROM provenance p WHERE p.domain_id = :d AND p.id IN (
        SELECT e.value FROM changesets c
        JOIN lb_gc_dead d ON d.id = c.revision_id, LATERAL jsonb_each_text({_PAIRS}) e
        WHERE c.domain_id = :d
        EXCEPT
        {_PROV_USED} AND NOT EXISTS (SELECT 1 FROM lb_gc_dead d WHERE d.id = c.revision_id))
    """
)
_DRY_CHANGESETS = text(
    "SELECT count(*) FROM changesets c JOIN lb_gc_dead d ON d.id = c.revision_id "
    "WHERE c.domain_id = :d"
)
_DRY_HISTORY = text(
    "SELECT count(*) FROM mat_history h JOIN lb_gc_dead d ON d.id = h.revision_id "
    "WHERE h.domain_id = :d"
)
_REF_NAMES = text("SELECT name FROM refs WHERE domain_id = :d ORDER BY name")
_TRY_LOCKS = text(
    "SELECT coalesce(bool_and(l), true) FROM (SELECT pg_try_advisory_xact_lock(k) AS l "
    "FROM unnest(CAST(:ks AS bigint[])) AS t(k) ORDER BY k) s"
).bindparams(bindparam("ks", type_=ARRAY(BigInteger())))


@dataclass(frozen=True, slots=True)
class _Content:
    """A content-addressed table swept by hash."""

    table: str
    marks: str
    size: str


_NODES = _Content("tree_nodes", "lb_gc_nodes", "octet_length(t.body)")
_OBJECTS = _Content("objects", "lb_gc_objects", "t.size")


def _unmarked_page(content: _Content) -> Any:
    return text(
        f"SELECT t.hash FROM {content.table} t WHERE t.domain_id = :d AND t.hash > :last "
        f"AND NOT EXISTS (SELECT 1 FROM {content.marks} m WHERE m.hash = t.hash) "
        f"ORDER BY t.hash LIMIT :n"
    )


def _unmarked_total(content: _Content) -> Any:
    return text(
        f"SELECT count(*), coalesce(sum({content.size}), 0) FROM {content.table} t "
        f"WHERE t.domain_id = :d "
        f"AND NOT EXISTS (SELECT 1 FROM {content.marks} m WHERE m.hash = t.hash)"
    )


def _delete_unmarked(content: _Content) -> Any:
    return text(
        f"WITH gone AS (DELETE FROM {content.table} t WHERE t.domain_id = :d "
        f"AND t.hash = ANY(CAST(:hs AS bytea[])) "
        f"AND NOT EXISTS (SELECT 1 FROM {content.marks} m WHERE m.hash = t.hash) "
        f"RETURNING {content.size} AS size) "
        f"SELECT count(*), coalesce(sum(size), 0) FROM gone"
    ).bindparams(_bytea_array("hs"))


class _Steps:
    """Transactions of a collection on one connection; nested use joins the open one."""

    def __init__(self, connection: AsyncConnection, domain_id: str) -> None:
        self._connection = connection
        self._domain = domain_id
        self._open = False

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[None]:
        if self._open:
            yield
            return
        self._open = True
        try:
            async with self._connection.begin():
                await scope_connection(self._connection, self._domain)
                yield
        finally:
            self._open = False


async def scope_connection(connection: AsyncConnection, domain_id: str) -> None:
    """Make the rows of `domain_id` visible to the current transaction (row-level security)."""
    if "," in domain_id:
        raise ValueError("Domain ids cannot contain commas")
    await connection.execute(
        text("SELECT set_config(:name, :domain, true)"),
        {"name": DOMAIN_IDS_SETTING, "domain": domain_id},
    )


class _Collector:
    def __init__(
        self,
        connection: AsyncConnection,
        domain_id: str,
        settings: GcSettings,
        *,
        dry_run: bool,
        cutoff: datetime,
        hook: PhaseHook | None,
    ) -> None:
        self._conn = connection
        self._domain = domain_id
        self._cfg = settings
        self._dry = dry_run
        self._cutoff = cutoff
        self._hook = hook
        self._step = _Steps(connection, domain_id)
        self._marker = Marker(connection, domain_id, batch=settings.mark_batch, step=self._step)
        self.report = GcReport(domain_id, dry_run, cutoff.isoformat())

    async def run(self) -> GcReport:
        report = self.report
        started = time.monotonic()
        await self._marker.prepare()
        await self._marker.mark(cutoff=self._cutoff)
        stats = await self._marker.stats()
        report.marked_revisions = stats.revisions
        report.marked_nodes = stats.nodes
        report.marked_objects = stats.objects
        report.missing_nodes = stats.missing_nodes
        report.mark_seconds = time.monotonic() - started
        if report.missing_nodes:
            log.warning(
                "Domain %s: %d reachable tree nodes are missing from the store",
                self._domain,
                report.missing_nodes,
            )
        await self._phase(PHASE_MARKED)
        swept = time.monotonic()
        await self._sweep_revisions()
        await self._phase(PHASE_REVISIONS_SWEPT)
        await self._sweep_content(_NODES)
        await self._phase(PHASE_NODES_SWEPT)
        await self._sweep_content(_OBJECTS)
        await self._phase(PHASE_OBJECTS_SWEPT)
        report.sweep_seconds = time.monotonic() - swept
        return report

    async def _phase(self, name: str) -> None:
        log.info("gc %s: %s", self._domain, name)
        if self._hook is not None:
            await self._hook(name)

    # Revisions --------------------------------------------------------------------

    async def _sweep_revisions(self) -> None:
        domain = {"d": self._domain}
        async with self._step():
            await self._conn.execute(_DEAD_FILL, domain)
        if self._dry:
            await self._count_revisions()
            return
        page = self._cfg.sweep_batch_rows
        after: tuple[int, bytes] | None = None
        while True:
            async with self._step():
                query = _DEAD_FIRST if after is None else _DEAD_NEXT
                params: dict[str, Any] = {"n": page}
                if after is not None:
                    params.update(g=after[0], i=after[1])
                rows = (await self._conn.execute(query, params)).all()
            if not rows:
                break
            after = (int(rows[-1][1]), bytes(rows[-1][0]))
            await self._guarded(partial(self._delete_revisions, [bytes(row[0]) for row in rows]))
        async with self._step():
            result = await self._conn.execute(_PROV_DELETE, domain)
            self.report.provenance = int(result.rowcount)

    async def _count_revisions(self) -> None:
        domain = {"d": self._domain}
        report = self.report
        async with self._step():
            report.revisions = int(
                (await self._conn.execute(text("SELECT count(*) FROM lb_gc_dead"))).scalar_one()
            )
            if not report.revisions:
                return
            report.changesets = int(
                (await self._conn.execute(_DRY_CHANGESETS, domain)).scalar_one()
            )
            report.history_rows = int((await self._conn.execute(_DRY_HISTORY, domain)).scalar_one())
            report.provenance = int((await self._conn.execute(_PROV_DRY, domain)).scalar_one())

    async def _delete_revisions(self, ids: list[bytes]) -> None:
        """Delete the still unmarked Revisions among `ids` (inside a guarded transaction)."""
        domain = {"d": self._domain}
        doomed = [bytes(r) for r in (await self._conn.execute(_DEAD_STILL, {"ids": ids})).scalars()]
        if not doomed:
            return
        params = {**domain, "ids": doomed}
        await self._conn.execute(_PROV_COLLECT, params)
        report = self.report
        report.history_rows += int((await self._conn.execute(_HISTORY_DELETE, params)).rowcount)
        await self._conn.execute(_PARENTS_DELETE, params)
        report.changesets += int((await self._conn.execute(_CHANGESETS_DELETE, params)).rowcount)
        report.revisions += int((await self._conn.execute(_REVISIONS_DELETE, params)).rowcount)

    # Nodes and objects ------------------------------------------------------------

    async def _sweep_content(self, content: _Content) -> None:
        domain = {"d": self._domain}
        if self._dry:
            async with self._step():
                count, size = (await self._conn.execute(_unmarked_total(content), domain)).one()
            self._add(content, int(count), int(size))
            return
        page_query = _unmarked_page(content)
        delete_query = _delete_unmarked(content)
        last = b""
        while True:
            async with self._step():
                page = [
                    bytes(h)
                    for h in (
                        await self._conn.execute(
                            page_query, {**domain, "last": last, "n": self._cfg.sweep_batch_rows}
                        )
                    ).scalars()
                ]
            if not page:
                break
            last = page[-1]

            async def delete(hashes: list[bytes] = page) -> None:
                count, size = (
                    await self._conn.execute(delete_query, {**domain, "hs": hashes})
                ).one()
                self._add(content, int(count), int(size))

            await self._guarded(delete)

    def _add(self, content: _Content, count: int, size: int) -> None:
        if content is _NODES:
            self.report.nodes += count
            self.report.node_bytes += size
        else:
            self.report.objects += count
            self.report.object_bytes += size

    # Guard ------------------------------------------------------------------------

    async def _guarded(self, body: Callable[[], Awaitable[None]]) -> None:
        """Run `body` in a transaction holding every branch lock, after marking again.

        Waits (retrying without holding anything) while a commit holds a branch lock.
        """
        deadline = time.monotonic() + self._cfg.guard_wait_seconds
        while True:
            try:
                async with self._step():
                    await self._lock_branches()
                    await self._marker.mark(attempts=True)
                    await body()
                return
            except _BranchBusyError:
                if time.monotonic() >= deadline:
                    raise GcError(
                        f"Domain {self._domain!r}: commits held branch locks for "
                        f"{self._cfg.guard_wait_seconds} s; collection stopped, "
                        "progress so far is kept"
                    ) from None
                await asyncio.sleep(self._cfg.guard_retry_ms / 1000)

    async def _lock_branches(self) -> None:
        if not self._cfg.guard_branch_locks:
            return
        locked: set[str] = set()
        for _ in range(3):
            names = [
                str(n)
                for n in (await self._conn.execute(_REF_NAMES, {"d": self._domain})).scalars()
            ]
            if len(names) > self._cfg.max_guarded_branches:
                raise GcError(
                    f"Domain {self._domain!r} has {len(names)} refs, more than "
                    f"gc.max_guarded_branches ({self._cfg.max_guarded_branches})"
                )
            fresh = [n for n in names if n not in locked]
            if not fresh:
                return
            keys = sorted(advisory_lock_key(self._domain, n) for n in fresh)
            if not (await self._conn.execute(_TRY_LOCKS, {"ks": keys})).scalar_one():
                raise _BranchBusyError
            locked.update(fresh)
        raise _BranchBusyError  # refs keep appearing; try again


# --------------------------------------------------------------------------------------
# Entry points


async def collect_garbage(
    engine: AsyncEngine,
    domain_id: str,
    *,
    runtime: StoreRuntime | None = None,
    dry_run: bool = False,
    grace_seconds: int | None = None,
    now: datetime | None = None,
    hook: PhaseHook | None = None,
) -> GcReport:
    """Collect the garbage of one Domain, or report what would go with `dry_run`.

    Revisions created less than `grace_seconds` (default: `store.gc.grace_seconds`) before
    `now` (default: the current time) are kept with everything they reach. Raises
    `DomainNotFoundError`, `GcBusyError` when another collection of the Domain runs, and
    `GcError` when commits hold branch locks for too long.
    """
    settings = (runtime or default_runtime()).settings.gc
    grace = settings.grace_seconds if grace_seconds is None else grace_seconds
    cutoff = (now or datetime.now(UTC)) - timedelta(seconds=grace)
    key = gc_lock_key(domain_id)
    async with engine.connect() as connection:
        async with connection.begin():
            found = await connection.execute(
                text("SELECT 1 FROM domains WHERE id = :d"), {"d": domain_id}
            )
            if found.first() is None:
                raise DomainNotFoundError(domain_id)
            acquired = await connection.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
            if not acquired.scalar_one():
                raise GcBusyError(f"a collection of Domain {domain_id!r} is already running")
        collector = _Collector(
            connection, domain_id, settings, dry_run=dry_run, cutoff=cutoff, hook=hook
        )
        try:
            return await collector.run()
        finally:
            if connection.in_transaction():
                await connection.rollback()
            async with connection.begin():
                await connection.execute(text("DROP TABLE IF EXISTS " + ", ".join(_MARK_TABLES)))
                await connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})


async def run_gc_job(
    engine: AsyncEngine, job: JobRow, *, runtime: StoreRuntime | None = None
) -> dict[str, Any]:
    """Run a `gc` job: payload `{"dry_run": bool, "grace_seconds": int}` (both optional)."""
    if job.domain_id is None:
        raise GcError("a gc job needs a domain_id")
    report = await collect_garbage(
        engine,
        job.domain_id,
        runtime=runtime,
        dry_run=bool(job.payload.get("dry_run", False)),
        grace_seconds=job.payload.get("grace_seconds"),
    )
    return report.as_dict()


async def enqueue_gc(
    session: AsyncSession,
    domain_id: str,
    *,
    dry_run: bool = False,
    grace_seconds: int | None = None,
) -> int:
    """Queue a `gc` job for a Domain; an identical queued or running job is reused."""
    from lorebase_store.jobs import GC_JOB, enqueue_job

    payload: dict[str, Any] = {"dry_run": dry_run}
    if grace_seconds is not None:
        payload["grace_seconds"] = grace_seconds
    return await enqueue_job(session, GC_JOB, domain_id=domain_id, payload=payload, unique=True)


# --------------------------------------------------------------------------------------
# Command line


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lorebase_store.gc",
        description="Garbage collection of unreachable Revisions, tree nodes and objects. "
        "Reads the database and store.gc settings from the Lorebase configuration.",
    )
    parser.add_argument("--domain", action="append", default=[], help="Domain id (repeatable).")
    parser.add_argument("--all", action="store_true", help="Every Domain of the installation.")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be deleted.")
    parser.add_argument(
        "--grace-seconds", type=int, default=None, help="Override store.gc.grace_seconds."
    )
    parser.add_argument(
        "--enqueue", action="store_true", help="Queue a gc job per Domain instead of running."
    )
    return parser


async def _main(args: argparse.Namespace) -> int:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from lorebase_platform.settings import load_settings
    from lorebase_store.db import create_engine
    from lorebase_store.repositories.domains import DomainRepository
    from lorebase_store.settings import database_settings

    settings = load_settings()
    runtime = StoreRuntime.from_loaded(settings)
    engine = create_engine(database_settings(settings), pooled=False, statement_timeout_ms=0)
    status = 0
    try:
        domains: list[str] = list(args.domain)
        if args.all:
            async with async_sessionmaker(engine)() as session:
                domains = [row.id for row in await DomainRepository(session).list(limit=100000)]
        if not domains:
            raise SystemExit("name a Domain with --domain, or use --all")
        for domain in domains:
            if args.enqueue:
                async with async_sessionmaker(engine)() as session, session.begin():
                    job = await enqueue_gc(
                        session, domain, dry_run=args.dry_run, grace_seconds=args.grace_seconds
                    )
                sys.stdout.write(json.dumps({"domain_id": domain, "job_id": job}) + "\n")
                continue
            try:
                report = await collect_garbage(
                    engine,
                    domain,
                    runtime=runtime,
                    dry_run=args.dry_run,
                    grace_seconds=args.grace_seconds,
                )
            except (GcError, DomainNotFoundError) as error:
                sys.stderr.write(f"{domain}: {error}\n")
                status = 1
                continue
            sys.stdout.write(json.dumps(report.as_dict()) + "\n")
    finally:
        await engine.dispose()
    return status


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main(_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
