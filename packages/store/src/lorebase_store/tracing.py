"""Steps of traced agent sessions (`agent_trace_steps`, `keeper.trace`).

`TraceRepository` appends steps in batches (at most `max_per_session` steps per session and
actor are kept: later ones are not written), lists sessions and reads the timeline of one,
and prunes steps older than the retention period. A step without a session id
(`inferred`) joins the inferred session of its actor and client fingerprint that saw a step
within the idle timeout, or starts a new one. Every statement runs with row-level
security bypassed: what a caller may see is decided by `TraceVisibility`, which the caller
builds from its permissions.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Select,
    and_,
    cast,
    delete,
    func,
    insert,
    literal,
    null,
    or_,
    select,
    text,
    true,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.auth.scope import rls_scope
from lorebase_store.models import AgentTraceStep

INFERRED_PREFIX = "inferred-"
"""Prefix of the ids of inferred sessions."""


@dataclass(frozen=True, slots=True)
class NewTraceStep:
    """A step to write. `type` is `call` (a traced request) or `note`."""

    session_id: str
    actor: str
    type: str
    at: datetime
    principal_type: str | None = None
    domain_id: str | None = None
    tool: str | None = None
    method: str | None = None
    route: str | None = None
    status: int | None = None
    latency_ms: float | None = None
    cache: str | None = None
    branch: str | None = None
    query: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    refs: dict[str, Any] | None = None
    explain: dict[str, Any] | None = None
    note: str | None = None
    proposal_id: str | None = None
    request_id: str | None = None
    trace_id: str | None = None
    inferred: bool = False
    """`session_id` is assigned on write: the inferred session of `fingerprint` that saw a step
    within the idle timeout, or a new one."""
    fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class TraceStepRecord(NewTraceStep):
    """A stored step; `seq` orders steps written at the same instant."""

    seq: int = 0


@dataclass(frozen=True, slots=True)
class TraceSessionSummary:
    """One session of one actor, over the steps the reader may see."""

    session_id: str
    actor: str
    started_at: datetime
    last_at: datetime
    calls: int
    notes: int
    domains: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    proposals: tuple[str, ...] = ()
    inferred: bool = False


@dataclass(frozen=True, slots=True)
class TraceVisibility:
    """Which steps a reader sees: its own (`owner`), and those of others in `domains` (None:
    every Domain), plus steps outside any Domain (notes) when `domainless`. `owner_domains`
    limits the reader's own steps to those Domains and to steps outside any Domain (an API key
    limited to Domains); None: no limit."""

    owner: str
    domains: frozenset[str] | None = field(default_factory=frozenset)
    domainless: bool = False
    owner_domains: frozenset[str] | None = None

    def condition(self) -> ColumnElement[bool]:
        t = AgentTraceStep
        others: list[ColumnElement[bool]] = []
        if self.domains is None:
            others.append(t.domain_id.is_not(None))
        elif self.domains:
            others.append(t.domain_id.in_(sorted(self.domains)))
        if self.domainless:
            others.append(t.domain_id.is_(None))
        own: ColumnElement[bool] = t.actor == self.owner
        if self.owner_domains is not None:
            own = and_(
                own,
                or_(t.domain_id.is_(None), t.domain_id.in_(sorted(self.owner_domains))),
            )
        return or_(own, *others) if others else own


_FIELDS = tuple(name for name in NewTraceStep.__dataclass_fields__)


def _record(row: AgentTraceStep) -> TraceStepRecord:
    return TraceStepRecord(seq=row.seq, **{name: getattr(row, name) for name in _FIELDS})


class TraceRepository:
    """Reads and writes `agent_trace_steps` (see the module doc)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_many(
        self,
        steps: Sequence[NewTraceStep],
        *,
        max_per_session: int,
        inferred_idle_s: float = 300.0,
    ) -> int:
        """Write the steps in order, skipping those of sessions (per actor) that already hold
        `max_per_session` steps; returns the number written. Inferred steps get their session
        here (`_inferred_session`)."""
        t = AgentTraceStep
        written = 0
        async with rls_scope(self._session, bypass=True):
            for step in steps:
                if step.inferred:
                    step = replace(
                        step, session_id=await self._inferred_session(step, inferred_idle_s)
                    )
                held = (
                    select(func.count())
                    .select_from(t)
                    .where(t.actor == step.actor, t.session_id == step.session_id)
                    .scalar_subquery()
                )
                values = {name: getattr(step, name) for name in _FIELDS}
                columns = t.__table__.c
                source = select(
                    *(
                        cast(null(), columns[k].type) if v is None else literal(v, columns[k].type)
                        for k, v in values.items()
                    )
                )
                statement = insert(t).from_select(
                    list(values), source.where(held < max_per_session)
                )
                result = await self._session.execute(statement)
                written += int(getattr(result, "rowcount", 0) or 0)
        return written

    async def _inferred_session(self, step: NewTraceStep, idle_s: float) -> str:
        """The session of the newest inferred step of the same actor and fingerprint at most
        `idle_s` before `step`, or a new session id. Serialized per actor and fingerprint, so
        Keepers writing at the same time agree."""
        t = AgentTraceStep
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"trace\x1f{step.actor}\x1f{step.fingerprint}"},
        )
        found = (
            await self._session.execute(
                select(t.session_id)
                .where(
                    t.inferred,
                    t.actor == step.actor,
                    t.fingerprint == step.fingerprint,
                    t.at >= step.at - timedelta(seconds=idle_s),
                    t.at <= step.at,
                )
                .order_by(t.at.desc(), t.seq.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return found or f"{INFERRED_PREFIX}{uuid.uuid4().hex[:16]}"

    async def prune(self, before: datetime, *, limit: int) -> int:
        """Delete up to `limit` steps older than `before`; the number deleted."""
        t = AgentTraceStep
        oldest = select(t.seq).where(t.at < before).order_by(t.at).limit(limit)
        async with rls_scope(self._session, bypass=True):
            result = await self._session.execute(delete(t).where(t.seq.in_(oldest)))
        return int(getattr(result, "rowcount", 0) or 0)

    async def sessions(
        self,
        visibility: TraceVisibility,
        *,
        limit: int,
        actor: str | None = None,
        domain_id: str | None = None,
        after: tuple[datetime, str, str] | None = None,
    ) -> list[TraceSessionSummary]:
        """Sessions with a visible step, most recent activity first. `after` is the
        `(last_at, session_id, actor)` of the last session of the previous page."""
        t = AgentTraceStep
        conditions = [visibility.condition()]
        if actor is not None:
            conditions.append(t.actor == actor)
        if domain_id is not None:
            conditions.append(t.domain_id == domain_id)
        last_at = func.max(t.at)
        statement: Select[Any] = (
            select(
                t.session_id,
                t.actor,
                func.min(t.at).label("started_at"),
                last_at.label("last_at"),
                func.count().filter(t.type == "call").label("calls"),
                func.count().filter(t.type == "note").label("notes"),
                func.bool_or(t.inferred).label("inferred"),
                func.array_remove(func.array_agg(t.domain_id.distinct()), None).label("domains"),
                func.array_remove(func.array_agg(t.tool.distinct()), None).label("tools"),
                func.array_remove(func.array_agg(t.proposal_id.distinct()), None).label(
                    "proposals"
                ),
            )
            .where(*conditions)
            .group_by(t.session_id, t.actor)
            .order_by(last_at.desc(), t.session_id.desc(), t.actor.desc())
            .limit(limit)
        )
        if after is not None:
            statement = statement.having(tuple_(last_at, t.session_id, t.actor) < tuple_(*after))
        async with rls_scope(self._session, bypass=True):
            rows = (await self._session.execute(statement)).mappings().all()
        return [
            TraceSessionSummary(
                session_id=row["session_id"],
                actor=row["actor"],
                started_at=row["started_at"],
                last_at=row["last_at"],
                calls=int(row["calls"]),
                notes=int(row["notes"]),
                domains=tuple(sorted(row["domains"] or ())),
                tools=tuple(sorted(row["tools"] or ())),
                proposals=tuple(sorted(row["proposals"] or ())),
                inferred=bool(row["inferred"]),
            )
            for row in rows
        ]

    async def steps(
        self,
        session_id: str,
        visibility: TraceVisibility,
        *,
        limit: int,
        actor: str | None = None,
    ) -> list[TraceStepRecord]:
        """The visible steps of a session in order (time, then write order), at most
        `limit`."""
        t = AgentTraceStep
        statement = (
            select(t)
            .where(
                t.session_id == session_id,
                visibility.condition(),
                t.actor == actor if actor is not None else true(),
            )
            .order_by(t.at, t.seq)
            .limit(limit)
        )
        async with rls_scope(self._session, bypass=True):
            rows = (await self._session.execute(statement)).scalars().all()
        return [_record(row) for row in rows]
