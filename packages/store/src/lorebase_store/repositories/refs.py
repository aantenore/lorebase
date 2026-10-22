"""Refs: branch and tag heads, compare-and-swap moves, the branch lock and the ref log.

Design 6.5: writers to one branch are serialized by a transaction-scoped advisory lock on a
stable 64-bit hash of `(domain_id, branch)`; the head moves by compare-and-swap on
`(head, version)`.

A branch being deleted in the background (`deleted_at` set, `mark_deleted`) is invisible to
every method here except `get_deleting`, `delete` and `dependents`: reads, listings, counts
and overlay lookups skip it, and its name stays taken until `delete` removes the row.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Table, delete, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.jobs import (
    INDEX_JOB,
    MATERIALIZE_JOB,
    SENSITIVITY_REFRESH_JOB,
    cancel_queued_jobs,
)
from lorebase_store.layout import BranchLayout, layout_from
from lorebase_store.models import metadata
from lorebase_store.models.enums import Materialization, RefKind
from lorebase_store.repositories.deferred_events import DeferredEventRepository

REFS: Table = metadata.tables["refs"]
REF_LOG: Table = metadata.tables["ref_log"]

_LIVE = REFS.c.deleted_at.is_(None)
"""Refs whose deletion is not in progress."""

LOCK_NOT_AVAILABLE = "55P03"
"""SQLSTATE raised when `lock_timeout` expires."""

_LOCK_KEY_SCOPE = b"lb1\x00ref-lock\x00"


def advisory_lock_key(domain_id: str, name: str) -> int:
    """Stable signed 64-bit advisory lock key of a ref."""
    digest = hashlib.sha256(
        _LOCK_KEY_SCOPE + domain_id.encode("utf-8") + b"\x00" + name.encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def sqlstate(error: BaseException) -> str | None:
    """SQLSTATE of a database error raised through SQLAlchemy, if any."""
    current: BaseException | None = error
    while current is not None:
        code = getattr(current, "sqlstate", None)
        if isinstance(code, str):
            return code
        current = getattr(current, "orig", None) or current.__cause__
    return None


class RefNotFoundError(LookupError):
    """The ref does not exist."""


class RefMovedError(Exception):
    """Compare-and-swap failed: the ref no longer has the expected head and version."""

    def __init__(self, domain_id: str, name: str, expected_head: bytes, expected_version: int):
        super().__init__(
            f"ref {name!r} of Domain {domain_id!r} moved from {expected_head.hex()} "
            f"version {expected_version}"
        )
        self.domain_id = domain_id
        self.name = name
        self.expected_head = expected_head
        self.expected_version = expected_version


class LockTimeoutError(Exception):
    """The branch lock was not acquired within the configured time."""


@dataclass(frozen=True, slots=True)
class RefRow:
    domain_id: str
    name: str
    kind: RefKind
    head: bytes
    protected: bool
    materialization: Materialization
    version: int
    created_at: datetime
    created_by: str
    base_branch: str | None = None
    base_revision: bytes | None = None
    base_generation: int | None = None
    deleted_at: datetime | None = None

    @property
    def layout(self) -> BranchLayout:
        """Where the branch's materialized rows live (its own, or an overlay on a base)."""
        return layout_from(
            self.domain_id,
            self.name,
            self.materialization,
            self.base_branch,
            self.base_generation,
        )


@dataclass(frozen=True, slots=True)
class RefLogRow:
    ref_name: str
    seq: int
    old_head: bytes | None
    new_head: bytes
    reason: str
    actor: str
    at: datetime


class RefRepository:
    """Refs of one Domain."""

    def __init__(self, session: AsyncSession, domain_id: str) -> None:
        self._session = session
        self._domain = domain_id

    async def get(self, name: str) -> RefRow | None:
        result = await self._session.execute(
            select(REFS).where(REFS.c.domain_id == self._domain, REFS.c.name == name, _LIVE)
        )
        found = result.mappings().one_or_none()
        return None if found is None else RefRow(**found)

    async def get_deleting(self, name: str, *, key_share: bool = False) -> RefRow | None:
        """The ref named `name` whether or not its deletion is in progress. With `key_share`,
        the row is locked `FOR KEY SHARE`, so it cannot be deleted before the caller's
        transaction ends (commits still move it)."""
        query = select(REFS).where(REFS.c.domain_id == self._domain, REFS.c.name == name)
        if key_share:
            query = query.with_for_update(key_share=True)
        found = (await self._session.execute(query)).mappings().one_or_none()
        return None if found is None else RefRow(**found)

    async def require(self, name: str) -> RefRow:
        found = await self.get(name)
        if found is None:
            raise RefNotFoundError(f"{self._domain}:{name}")
        return found

    async def all(self, kind: RefKind | None = None) -> list[RefRow]:
        query = select(REFS).where(REFS.c.domain_id == self._domain, _LIVE).order_by(REFS.c.name)
        if kind is not None:
            query = query.where(REFS.c.kind == kind)
        result = await self._session.execute(query)
        return [RefRow(**row) for row in result.mappings()]

    async def page(self, kind: RefKind, *, limit: int, after: str | None = None) -> list[RefRow]:
        """Refs of one kind ordered by name, starting after `after`."""
        query = (
            select(REFS)
            .where(REFS.c.domain_id == self._domain, REFS.c.kind == kind, _LIVE)
            .order_by(REFS.c.name)
            .limit(limit)
        )
        if after is not None:
            query = query.where(REFS.c.name > after)
        result = await self._session.execute(query)
        return [RefRow(**row) for row in result.mappings()]

    async def count(self, kind: RefKind, materialization: Materialization | None = None) -> int:
        """Number of refs of one kind, optionally with one materialization mode."""
        query = (
            select(func.count())
            .select_from(REFS)
            .where(REFS.c.domain_id == self._domain, REFS.c.kind == kind, _LIVE)
        )
        if materialization is not None:
            query = query.where(REFS.c.materialization == materialization)
        return int(await self._session.scalar(query) or 0)

    async def set_protected(self, name: str, protected: bool) -> RefRow | None:
        """Set the protection flag of a branch; returns the ref, `None` when it does not exist."""
        await self._session.execute(
            update(REFS)
            .where(REFS.c.domain_id == self._domain, REFS.c.name == name)
            .values(protected=protected)
        )
        return await self.get(name)

    async def delete(self, name: str) -> bool:
        """Remove a ref (also one whose deletion is in progress). Materialized rows of a branch
        go with it; the ref log is kept.

        In the same transaction, the queued index builds, materializations and sensitivity
        refreshes of the branch are cancelled: there is nothing left to build. Queued drops
        stay, they clean up the branch's indexes. Operation events still deferred on the branch
        (not merged) go too.
        """
        result = await self._session.execute(
            delete(REFS)
            .where(REFS.c.domain_id == self._domain, REFS.c.name == name)
            .returning(REFS.c.name)
        )
        if result.scalar_one_or_none() is None:
            return False
        await self._cancel_jobs(name)
        await DeferredEventRepository(self._session, self._domain).discard(name)
        return True

    async def mark_deleted(self, name: str, *, at: datetime) -> bool:
        """Hide a branch whose rows a background job deletes (see the module doc); cancels
        its queued index builds and materializations like `delete`."""
        result = await self._session.execute(
            update(REFS)
            .where(REFS.c.domain_id == self._domain, REFS.c.name == name, _LIVE)
            .values(deleted_at=at)
            .returning(REFS.c.name)
        )
        if result.scalar_one_or_none() is None:
            return False
        await self._cancel_jobs(name)
        return True

    async def dependents(self, base: str) -> list[RefRow]:
        """Every ref naming `base` as its base branch, deletions in progress included."""
        result = await self._session.execute(
            select(REFS)
            .where(REFS.c.domain_id == self._domain, REFS.c.base_branch == base)
            .order_by(REFS.c.name)
        )
        return [RefRow(**row) for row in result.mappings()]

    async def _cancel_jobs(self, name: str) -> None:
        await cancel_queued_jobs(
            self._session,
            INDEX_JOB,
            domain_id=self._domain,
            match={"branch": name, "action": "create"},
            reason="branch deleted",
        )
        await cancel_queued_jobs(
            self._session,
            MATERIALIZE_JOB,
            domain_id=self._domain,
            match={"branch": name},
            reason="branch deleted",
        )
        await cancel_queued_jobs(
            self._session,
            SENSITIVITY_REFRESH_JOB,
            domain_id=self._domain,
            match={"branch": name},
            reason="branch deleted",
        )

    async def create(
        self,
        name: str,
        head: bytes,
        *,
        created_at: datetime,
        created_by: str,
        kind: RefKind = RefKind.BRANCH,
        materialization: Materialization = Materialization.FULL,
        protected: bool = False,
        reason: str = "create",
        base_branch: str | None = None,
        base_revision: bytes | None = None,
        base_generation: int | None = None,
        log: bool = True,
    ) -> RefRow:
        """Insert a ref at version 1 and, unless `log` is false, log its creation (overlays
        name their base)."""
        row = RefRow(
            domain_id=self._domain,
            name=name,
            kind=kind,
            head=head,
            protected=protected,
            materialization=materialization,
            version=1,
            created_at=created_at,
            created_by=created_by,
            base_branch=base_branch,
            base_revision=base_revision,
            base_generation=base_generation,
        )
        await self._session.execute(
            insert(REFS).values(
                domain_id=row.domain_id,
                name=row.name,
                kind=row.kind,
                head=row.head,
                protected=row.protected,
                materialization=row.materialization,
                version=row.version,
                created_at=row.created_at,
                created_by=row.created_by,
                base_branch=row.base_branch,
                base_revision=row.base_revision,
                base_generation=row.base_generation,
            )
        )
        if log:
            await self._append_log(name, None, head, reason=reason, actor=created_by, at=created_at)
        return row

    async def overlays_of(self, base: str) -> list[RefRow]:
        """Overlay branches whose base is `base`, by name."""
        result = await self._session.execute(
            select(REFS)
            .where(
                REFS.c.domain_id == self._domain,
                REFS.c.base_branch == base,
                REFS.c.materialization == Materialization.OVERLAY,
                _LIVE,
            )
            .order_by(REFS.c.name)
        )
        return [RefRow(**row) for row in result.mappings()]

    async def set_materialization(
        self,
        name: str,
        materialization: Materialization,
        *,
        base_branch: str | None = None,
        base_revision: bytes | None = None,
        base_generation: int | None = None,
    ) -> None:
        """Change how a branch is materialized (promotion of an overlay, re-fork). The head
        and version stay: commits in flight on the branch keep their compare-and-swap."""
        await self._session.execute(
            update(REFS)
            .where(REFS.c.domain_id == self._domain, REFS.c.name == name)
            .values(
                materialization=materialization,
                base_branch=base_branch,
                base_revision=base_revision,
                base_generation=base_generation,
            )
        )

    async def lock(self, name: str, *, timeout_ms: int) -> None:
        """Take the transaction-scoped advisory lock of a ref, waiting at most `timeout_ms`.

        `timeout_ms = 0` waits without limit. Raises `LockTimeoutError`; the transaction is then
        aborted and must be rolled back.
        """
        key = advisory_lock_key(self._domain, name)
        if timeout_ms <= 0:
            await self._session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
            return
        previous = await self._session.scalar(text("SELECT current_setting('lock_timeout')"), {})
        await self._set_lock_timeout(f"{timeout_ms}ms")
        try:
            await self._session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
        except DBAPIError as error:
            if sqlstate(error) == LOCK_NOT_AVAILABLE:
                raise LockTimeoutError(
                    f"branch {name!r} of Domain {self._domain!r} stayed locked for {timeout_ms} ms"
                ) from error
            raise
        await self._set_lock_timeout(str(previous))

    async def compare_and_swap(
        self,
        name: str,
        *,
        expected_head: bytes,
        expected_version: int,
        new_head: bytes,
        actor: str,
        at: datetime,
        reason: str,
    ) -> int:
        """Move the ref from `(expected_head, expected_version)` to `new_head`.

        Returns the new version; raises `RefMovedError` when the ref has moved meanwhile.
        """
        result = await self._session.execute(
            update(REFS)
            .where(
                REFS.c.domain_id == self._domain,
                REFS.c.name == name,
                REFS.c.head == expected_head,
                REFS.c.version == expected_version,
            )
            .values(head=new_head, version=REFS.c.version + 1)
            .returning(REFS.c.version)
        )
        version = result.scalar_one_or_none()
        if version is None:
            raise RefMovedError(self._domain, name, expected_head, expected_version)
        await self._append_log(name, expected_head, new_head, reason=reason, actor=actor, at=at)
        return int(version)

    async def log(self, name: str) -> list[RefLogRow]:
        """Head movements of a ref, oldest first."""
        result = await self._session.execute(
            select(
                REF_LOG.c.ref_name,
                REF_LOG.c.seq,
                REF_LOG.c.old_head,
                REF_LOG.c.new_head,
                REF_LOG.c.reason,
                REF_LOG.c.actor,
                REF_LOG.c.at,
            )
            .where(REF_LOG.c.domain_id == self._domain, REF_LOG.c.ref_name == name)
            .order_by(REF_LOG.c.seq)
        )
        return [RefLogRow(**row) for row in result.mappings()]

    async def _append_log(
        self,
        name: str,
        old_head: bytes | None,
        new_head: bytes,
        *,
        reason: str,
        actor: str,
        at: datetime,
    ) -> None:
        # The ref log outlives deleted refs, so the sequence continues after the last entry.
        next_seq = (
            select(func.coalesce(func.max(REF_LOG.c.seq), 0) + 1)
            .where(REF_LOG.c.domain_id == self._domain, REF_LOG.c.ref_name == name)
            .scalar_subquery()
        )
        await self._session.execute(
            insert(REF_LOG).values(
                domain_id=self._domain,
                ref_name=name,
                seq=next_seq,
                old_head=old_head,
                new_head=new_head,
                reason=reason,
                actor=actor,
                at=at,
            )
        )

    async def _set_lock_timeout(self, value: str) -> None:
        await self._session.execute(
            text("SELECT set_config('lock_timeout', :value, true)"), {"value": value}
        )
