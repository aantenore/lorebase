"""Revisions, their ordered parents and generation numbers (design 6.4)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Table, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from lorebase_core.revision import (
    Revision,
    UnknownRevisionError,
    next_generation,
    revision_id,
)
from lorebase_store.models import metadata

REVISIONS: Table = metadata.tables["revisions"]
REVISION_PARENTS: Table = metadata.tables["revision_parents"]

_FIRST_PARENT_CHAIN = text(
    """
    WITH RECURSIVE chain(id, depth) AS (
        SELECT CAST(:head AS bytea), 0
        UNION ALL
        SELECT p.parent_id, chain.depth + 1
        FROM chain
        JOIN revision_parents p
          ON p.domain_id = :domain AND p.revision_id = chain.id AND p.position = 0
    )
    SELECT id FROM chain ORDER BY depth
    """
)


_AHEAD_BEHIND = text(
    """
    WITH RECURSIVE
    left_side(id) AS (
        SELECT CAST(:left AS bytea)
        UNION
        SELECT p.parent_id
        FROM left_side
        JOIN revision_parents p ON p.domain_id = :domain AND p.revision_id = left_side.id
    ),
    right_side(id) AS (
        SELECT CAST(:right AS bytea)
        UNION
        SELECT p.parent_id
        FROM right_side
        JOIN revision_parents p ON p.domain_id = :domain AND p.revision_id = right_side.id
    )
    SELECT
        (SELECT count(*) FROM left_side WHERE id NOT IN (SELECT id FROM right_side)),
        (SELECT count(*) FROM right_side WHERE id NOT IN (SELECT id FROM left_side))
    """
)


@dataclass(frozen=True, slots=True)
class RevisionRow:
    id: bytes
    root: bytes
    generation: int
    parents: tuple[bytes, ...]
    author: str
    on_behalf_of: str | None
    message: str
    origin: dict[str, Any]
    stats: dict[str, Any]
    created_at: datetime


class RevisionRepository:
    """Revisions of one Domain."""

    def __init__(self, session: AsyncSession, domain_id: str) -> None:
        self._session = session
        self._domain = domain_id

    async def insert(self, revision: Revision) -> RevisionRow:
        """Store a Revision whose parents are stored; returns it with id and generation."""
        if revision.domain != self._domain:
            raise ValueError(f"Revision of Domain {revision.domain!r} in {self._domain!r}")
        generations = await self.generations(revision.parents)
        missing = [p for p in revision.parents if p not in generations]
        if missing:
            raise UnknownRevisionError(missing[0].hex())
        row = RevisionRow(
            id=revision_id(revision),
            root=revision.root,
            generation=next_generation(generations.values()),
            parents=tuple(revision.parents),
            author=revision.author.principal,
            on_behalf_of=revision.author.on_behalf_of,
            message=revision.message,
            origin=revision.origin.model_dump(),
            stats=revision.stats.model_dump(),
            created_at=revision.created_at,
        )
        await self._session.execute(
            insert(REVISIONS).values(
                domain_id=self._domain,
                id=row.id,
                root=row.root,
                generation=row.generation,
                author=row.author,
                on_behalf_of=row.on_behalf_of,
                message=row.message,
                origin=row.origin,
                stats=row.stats,
                created_at=row.created_at,
            )
        )
        if row.parents:
            await self._session.execute(
                insert(REVISION_PARENTS),
                [
                    {
                        "domain_id": self._domain,
                        "revision_id": row.id,
                        "position": position,
                        "parent_id": parent,
                    }
                    for position, parent in enumerate(row.parents)
                ],
            )
        return row

    async def get(self, revision: bytes) -> RevisionRow | None:
        result = await self._session.execute(
            select(REVISIONS).where(
                REVISIONS.c.domain_id == self._domain, REVISIONS.c.id == revision
            )
        )
        found = result.mappings().one_or_none()
        if found is None:
            return None
        parents = await self.parents(revision)
        values = {k: v for k, v in found.items() if k != "domain_id"}
        return RevisionRow(parents=parents, **values)

    async def require(self, revision: bytes) -> RevisionRow:
        found = await self.get(revision)
        if found is None:
            raise UnknownRevisionError(revision.hex())
        return found

    async def parents(self, revision: bytes) -> tuple[bytes, ...]:
        result = await self._session.execute(
            select(REVISION_PARENTS.c.parent_id)
            .where(
                REVISION_PARENTS.c.domain_id == self._domain,
                REVISION_PARENTS.c.revision_id == revision,
            )
            .order_by(REVISION_PARENTS.c.position)
        )
        return tuple(result.scalars())

    async def generations(self, revisions: Iterable[bytes]) -> dict[bytes, int]:
        wanted = list(dict.fromkeys(revisions))
        if not wanted:
            return {}
        result = await self._session.execute(
            select(REVISIONS.c.id, REVISIONS.c.generation).where(
                REVISIONS.c.domain_id == self._domain, REVISIONS.c.id.in_(wanted)
            )
        )
        return {rev: int(generation) for rev, generation in result}

    async def ahead_behind(self, left: bytes, right: bytes) -> tuple[int, int]:
        """Revisions reachable from `left` but not from `right`, and the other way round."""
        if left == right:
            return 0, 0
        result = await self._session.execute(
            _AHEAD_BEHIND, {"left": left, "right": right, "domain": self._domain}
        )
        ahead, behind = result.one()
        return int(ahead), int(behind)

    async def first_parent_chain(self, head: bytes) -> list[bytes]:
        """`head` and its first-parent ancestors, newest first."""
        result = await self._session.execute(
            _FIRST_PARENT_CHAIN, {"head": head, "domain": self._domain}
        )
        return list(result.scalars())


class PgRevisionGraph:
    """Synchronous core `RevisionGraph` over a synchronous `Session` (see `PgNodeStore`)."""

    def __init__(self, session: Session, domain_id: str) -> None:
        self._session = session
        self._domain = domain_id
        self._parents: dict[bytes, tuple[bytes, ...]] = {}
        self._generations: dict[bytes, int] = {}
        self._roots: dict[bytes, bytes] = {}

    def parents(self, revision_id: bytes) -> Sequence[bytes]:
        if revision_id not in self._parents:
            self._load(revision_id)
        return self._parents[revision_id]

    def generation(self, revision_id: bytes) -> int:
        if revision_id not in self._generations:
            self._load(revision_id)
        return self._generations[revision_id]

    def root(self, revision_id: bytes) -> bytes:
        """Tree root of a Revision (the `revision_root` callable of a merge input)."""
        if revision_id not in self._roots:
            self._load(revision_id)
        return self._roots[revision_id]

    def _load(self, revision: bytes) -> None:
        found = self._session.execute(
            select(REVISIONS.c.generation, REVISIONS.c.root).where(
                REVISIONS.c.domain_id == self._domain, REVISIONS.c.id == revision
            )
        ).one_or_none()
        if found is None:
            raise UnknownRevisionError(revision.hex())
        generation, root = found
        self._roots[revision] = root
        parents = self._session.execute(
            select(REVISION_PARENTS.c.parent_id)
            .where(
                REVISION_PARENTS.c.domain_id == self._domain,
                REVISION_PARENTS.c.revision_id == revision,
            )
            .order_by(REVISION_PARENTS.c.position)
        ).scalars()
        self._generations[revision] = int(generation)
        self._parents[revision] = tuple(parents)
