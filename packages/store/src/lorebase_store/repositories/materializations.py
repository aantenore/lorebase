"""Progress of background branch copies (`branch_materializations`, revisions 0017, 0031).

A row exists for each branch a `store.materialize` job turns into a full copy
(`lorebase_store.materialize.background`): from the base rows of an overlay (`base`) or from
the tree of its head (`tree`). It holds the status, the phase and cursor where the next
batch starts, the rows done out of the rows to read, the failed jobs and the schema changes
to reconcile.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Table, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.models import metadata
from lorebase_store.repositories.refs import advisory_lock_key

MATERIALIZATIONS: Table = metadata.tables["branch_materializations"]

_FENCE = text("SELECT pg_advisory_xact_lock(:key)")
_FENCE_SHARED = text("SELECT pg_advisory_xact_lock_shared(:key)")


async def copy_fence(
    session: AsyncSession, domain_id: str, branch: str, *, shared: bool = False
) -> None:
    """Take the transaction-scoped fence between the batches of a background copy of `branch`
    (`shared`) and whatever removes or replaces its rows outside a commit (promotion, re-fork,
    rebuild, the switch to a full copy; exclusive). A batch inserts with `ON CONFLICT DO
    NOTHING`: one running while such a transaction deletes rows of the branch could insert a
    row again after the delete. Commits do not take it."""
    key = advisory_lock_key(domain_id, f"\x00copy-fence\x00{branch}")
    await session.execute(_FENCE_SHARED if shared else _FENCE, {"key": key})


class MaterializationStatus(StrEnum):
    """Where a background copy stands."""

    PENDING = "pending"
    """Queued, or waiting for a retry after a failed attempt."""
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    """Its job used every attempt; the branch stays a correct overlay."""


ACTIVE = (MaterializationStatus.PENDING, MaterializationStatus.RUNNING)


class MaterializationStrategy(StrEnum):
    """Where a background copy reads the rows it writes."""

    BASE = "base"
    """The base rows an overlay reads, as of its fork point."""
    TREE = "tree"
    """The tree of the branch head (a `building` branch)."""


class MaterializationPhase(StrEnum):
    """What the next batch does; `switch` makes the branch a full copy.

    `base`: `records`, `links`, `history`, `switch`. `tree`: `clear` (rows left by an
    earlier state), `tree_history`, `tree_rows`, `derived`, `paths`, `switch`. After the
    switch, a copy that got schema changes reconciles: `prune`, `derived`, `paths`,
    `search`.
    """

    RECORDS = "records"
    LINKS = "links"
    HISTORY = "history"
    CLEAR = "clear"
    TREE_HISTORY = "tree_history"
    TREE_ROWS = "tree_rows"
    SWITCH = "switch"
    PRUNE = "prune"
    DERIVED = "derived"
    PATHS = "paths"
    SEARCH = "search"
    DONE = "done"


@dataclass(frozen=True, slots=True)
class MaterializationState:
    branch: str
    status: MaterializationStatus
    phase: MaterializationPhase
    base_branch: str
    base_generation: int
    cursor: list[Any] | None
    rows_done: int
    rows_total: int | None
    error: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    strategy: MaterializationStrategy = MaterializationStrategy.BASE
    failures: int = 0
    reconcile: dict[str, Any] | None = None

    @property
    def active(self) -> bool:
        return self.status in ACTIVE


def _state(row: Any) -> MaterializationState:
    cursor = row["cursor"]
    return MaterializationState(
        branch=row["branch"],
        status=MaterializationStatus(row["status"]),
        phase=MaterializationPhase(row["phase"]),
        base_branch=row["base_branch"],
        base_generation=int(row["base_generation"]),
        cursor=None if cursor is None else list(cursor["key"]),
        rows_done=int(row["rows_done"]),
        rows_total=None if row["rows_total"] is None else int(row["rows_total"]),
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        finished_at=row["finished_at"],
        strategy=MaterializationStrategy(row["strategy"]),
        failures=int(row["failures"]),
        reconcile=row["reconcile"],
    )


class BranchMaterializationRepository:
    """Background copy progress of the branches of one Domain."""

    def __init__(self, session: AsyncSession, domain_id: str) -> None:
        self._session = session
        self._domain = domain_id

    async def get(self, branch: str, *, for_update: bool = False) -> MaterializationState | None:
        query = select(MATERIALIZATIONS).where(
            MATERIALIZATIONS.c.domain_id == self._domain, MATERIALIZATIONS.c.branch == branch
        )
        if for_update:
            query = query.with_for_update()
        found = (await self._session.execute(query)).mappings().one_or_none()
        return None if found is None else _state(found)

    async def of(self, branches: Collection[str]) -> dict[str, MaterializationState]:
        """The rows of `branches` that have one, by branch."""
        if not branches:
            return {}
        result = await self._session.execute(
            select(MATERIALIZATIONS).where(
                MATERIALIZATIONS.c.domain_id == self._domain,
                MATERIALIZATIONS.c.branch.in_(list(branches)),
            )
        )
        return {row["branch"]: _state(row) for row in result.mappings()}

    async def active(self, branch: str) -> bool:
        """Whether a background copy of `branch` is queued or running."""
        found = await self._session.scalar(
            select(func.count())
            .select_from(MATERIALIZATIONS)
            .where(
                MATERIALIZATIONS.c.domain_id == self._domain,
                MATERIALIZATIONS.c.branch == branch,
                MATERIALIZATIONS.c.status.in_([s.value for s in ACTIVE]),
            )
        )
        return bool(found)

    async def count_active(self) -> int:
        """Background copies queued or running in the Domain."""
        found = await self._session.scalar(
            select(func.count())
            .select_from(MATERIALIZATIONS)
            .where(
                MATERIALIZATIONS.c.domain_id == self._domain,
                MATERIALIZATIONS.c.status.in_([s.value for s in ACTIVE]),
            )
        )
        return int(found or 0)

    async def start(
        self,
        branch: str,
        *,
        base_branch: str,
        base_generation: int,
        status: MaterializationStatus,
        at: datetime,
        strategy: MaterializationStrategy = MaterializationStrategy.BASE,
        failures: int = 0,
        reconcile: dict[str, Any] | None = None,
    ) -> MaterializationState:
        """Start (or restart from the first row) the copy of `branch`: from its fork point
        (`base`) or from the tree of its head (`tree`)."""
        first = (
            MaterializationPhase.RECORDS
            if strategy is MaterializationStrategy.BASE
            else MaterializationPhase.CLEAR
        )
        values: dict[str, Any] = {
            "status": status.value,
            "phase": first.value,
            "strategy": strategy.value,
            "base_branch": base_branch,
            "base_generation": base_generation,
            "cursor": None,
            "rows_done": 0,
            "rows_total": None,
            "error": None,
            "failures": failures,
            "reconcile": reconcile,
            "updated_at": at,
            "finished_at": None,
        }
        statement = pg_insert(MATERIALIZATIONS).values(
            domain_id=self._domain, branch=branch, created_at=at, **values
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[MATERIALIZATIONS.c.domain_id, MATERIALIZATIONS.c.branch],
                set_=values,
            )
        )
        found = await self.get(branch)
        assert found is not None
        return found

    async def update(self, branch: str, *, at: datetime, **values: Any) -> None:
        """Set columns of the row of `branch` (`cursor` as a list of key values or None)."""
        if "status" in values:
            values["status"] = MaterializationStatus(values["status"]).value
        if "phase" in values:
            values["phase"] = MaterializationPhase(values["phase"]).value
        if "strategy" in values:
            values["strategy"] = MaterializationStrategy(values["strategy"]).value
        if "cursor" in values and values["cursor"] is not None:
            values["cursor"] = {"key": list(values["cursor"])}
        await self._session.execute(
            update(MATERIALIZATIONS)
            .where(
                MATERIALIZATIONS.c.domain_id == self._domain,
                MATERIALIZATIONS.c.branch == branch,
            )
            .values(updated_at=at, **values)
        )

    async def finish(self, branch: str, *, at: datetime) -> None:
        """Mark the copy of `branch` done (also when another path made it a full copy)."""
        await self.update(
            branch,
            at=at,
            status=MaterializationStatus.DONE,
            phase=MaterializationPhase.DONE,
            cursor=None,
            error=None,
            failures=0,
            reconcile=None,
            finished_at=at,
        )

    async def add_reconcile(
        self, branch: str, *, refresh_kinds: Collection[str], at: datetime
    ) -> None:
        """Record a schema change made while the copy of `branch` runs: after its switch, the
        job recomputes what a schema change recomputes on a full branch (`refresh_kinds`:
        Kinds whose titles and search vectors change)."""
        state = await self.get(branch, for_update=True)
        if state is None:
            return
        kinds = set((state.reconcile or {}).get("refresh_kinds", ())) | set(refresh_kinds)
        await self.update(branch, at=at, reconcile={"refresh_kinds": sorted(kinds)})
