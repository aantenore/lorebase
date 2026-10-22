"""Steps of traced agent sessions (`agent_trace_steps`, `keeper.trace`).

`TraceRepository` appends steps in batches (at most `max_per_session` steps per session and
actor are kept: later ones are not written), lists sessions and reads the timeline of one,
and prunes steps older than the retention period. Each batch also keeps the session rows:
`agent_trace_sessions` counts the steps of a session (the cap, checked and raised under the
row lock in the writing transaction, so it holds across Keepers) and
`agent_trace_session_domains` summarizes them per Domain (sessions are listed from it);
pruning lowers both by what it deletes. A step without a session id (`inferred`) joins the
inferred session of its actor and client fingerprint with a step within the idle timeout of
it (before or after: concurrent requests finish out of order), or starts a new one. Every
statement runs with row-level security bypassed: what a caller may see is decided by
`TraceVisibility`, which the caller builds from its permissions.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Select,
    and_,
    delete,
    func,
    insert,
    or_,
    select,
    text,
    true,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from lorebase_store.auth.scope import rls_scope
from lorebase_store.models import AgentTraceSessionDomain, AgentTraceStep

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

    def condition(
        self,
        actor: InstrumentedAttribute[str] = AgentTraceStep.actor,
        domain_id: InstrumentedAttribute[str | None] = AgentTraceStep.domain_id,
    ) -> ColumnElement[bool]:
        """The visible rows, over the actor and Domain columns of the steps (default) or of
        the session summaries."""
        others: list[ColumnElement[bool]] = []
        if self.domains is None:
            others.append(domain_id.is_not(None))
        elif self.domains:
            others.append(domain_id.in_(sorted(self.domains)))
        if self.domainless:
            others.append(domain_id.is_(None))
        own: ColumnElement[bool] = actor == self.owner
        if self.owner_domains is not None:
            own = and_(
                own,
                or_(domain_id.is_(None), domain_id.in_(sorted(self.owner_domains))),
            )
        return or_(own, *others) if others else own


_FIELDS = tuple(name for name in NewTraceStep.__dataclass_fields__)


def _record(row: AgentTraceStep) -> TraceStepRecord:
    return TraceStepRecord(seq=row.seq, **{name: getattr(row, name) for name in _FIELDS})


def _union(lists: Sequence[Sequence[str]] | None) -> tuple[str, ...]:
    return tuple(sorted({item for items in lists or () for item in items}))


_INFERRED_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))")

_HOLD_SESSION = text(
    "INSERT INTO agent_trace_sessions AS s (actor, session_id, steps, last_at) "
    "VALUES (:actor, :session_id, 0, :at) "
    "ON CONFLICT (actor, session_id) DO UPDATE SET steps = s.steps "
    "RETURNING s.steps"
)
"""Creates the session row or locks it; returns the steps it holds."""

_GROW_SESSION = text(
    "UPDATE agent_trace_sessions SET steps = steps + :n, last_at = greatest(last_at, :at) "
    "WHERE actor = :actor AND session_id = :session_id"
)

_ADD_TO_DOMAIN = text(
    "INSERT INTO agent_trace_session_domains AS d (actor, session_id, domain_id, started_at, "
    "last_at, calls, notes, inferred, tools, proposals) "
    "VALUES (:actor, :session_id, :domain_id, :started_at, :last_at, :calls, :notes, "
    ":inferred, :tools, :proposals) "
    "ON CONFLICT (actor, session_id, domain_id) DO UPDATE SET "
    "started_at = least(d.started_at, excluded.started_at), "
    "last_at = greatest(d.last_at, excluded.last_at), "
    "calls = d.calls + excluded.calls, notes = d.notes + excluded.notes, "
    "inferred = d.inferred OR excluded.inferred, "
    "tools = ARRAY(SELECT DISTINCT u FROM unnest(d.tools || excluded.tools) AS u ORDER BY u), "
    "proposals = ARRAY(SELECT DISTINCT u FROM unnest(d.proposals || excluded.proposals) AS u "
    "ORDER BY u)"
)

_SHRINK_SESSION = text(
    "UPDATE agent_trace_sessions SET steps = steps - :n "
    "WHERE actor = :actor AND session_id = :session_id"
)

_DROP_SESSION = text(
    "DELETE FROM agent_trace_sessions "
    "WHERE actor = :actor AND session_id = :session_id AND steps <= 0"
)

_LOCK_DOMAIN = text(
    "SELECT 1 FROM agent_trace_session_domains "
    "WHERE actor = :actor AND session_id = :session_id "
    "AND domain_id IS NOT DISTINCT FROM :domain_id FOR UPDATE"
)

_SHRINK_DOMAIN = text(
    "UPDATE agent_trace_session_domains AS d SET calls = d.calls - :calls, "
    "notes = d.notes - :notes, "
    "started_at = coalesce(s.started_at, d.started_at), "
    "last_at = coalesce(s.last_at, d.last_at), "
    "inferred = coalesce(s.inferred, d.inferred), "
    "tools = coalesce(s.tools, d.tools), "
    "proposals = coalesce(s.proposals, d.proposals) "
    "FROM (SELECT min(t.at) AS started_at, max(t.at) AS last_at, "
    "bool_or(t.inferred) AS inferred, "
    "array_remove(array_agg(DISTINCT t.tool), NULL) AS tools, "
    "array_remove(array_agg(DISTINCT t.proposal_id), NULL) AS proposals "
    "FROM agent_trace_steps AS t WHERE t.actor = :actor AND t.session_id = :session_id "
    "AND t.domain_id IS NOT DISTINCT FROM :domain_id) AS s "
    "WHERE d.actor = :actor AND d.session_id = :session_id "
    "AND d.domain_id IS NOT DISTINCT FROM :domain_id"
)
"""Recomputes the summary from the steps left. Run after `_LOCK_DOMAIN`, so its snapshot
holds the steps of writers that updated the row before."""

_DROP_DOMAIN = text(
    "DELETE FROM agent_trace_session_domains "
    "WHERE actor = :actor AND session_id = :session_id "
    "AND domain_id IS NOT DISTINCT FROM :domain_id AND calls + notes <= 0"
)

_DomainKey = tuple[str, str, str | None]


@dataclass(slots=True)
class _DomainTally:
    """What a batch adds to the summary of one session in one Domain."""

    started_at: datetime
    last_at: datetime
    calls: int = 0
    notes: int = 0
    inferred: bool = False
    tools: set[str] = field(default_factory=set)
    proposals: set[str] = field(default_factory=set)

    def add(self, step: NewTraceStep) -> None:
        self.started_at = min(self.started_at, step.at)
        self.last_at = max(self.last_at, step.at)
        if step.type == "note":
            self.notes += 1
        else:
            self.calls += 1
        self.inferred = self.inferred or step.inferred
        if step.tool is not None:
            self.tools.add(step.tool)
        if step.proposal_id is not None:
            self.proposals.add(step.proposal_id)


def _domain_order(key: _DomainKey) -> tuple[str, str, bool, str]:
    """Lock order of summary rows (the null Domain first)."""
    actor, session_id, domain_id = key
    return actor, session_id, domain_id is not None, domain_id or ""


class TraceRepository:
    """Reads and writes `agent_trace_steps` and the session rows that count and summarize
    them (see the module doc)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_many(
        self,
        steps: Sequence[NewTraceStep],
        *,
        max_per_session: int,
        inferred_idle_s: float = 300.0,
    ) -> int:
        """Write the steps in order, skipping those past `max_per_session` steps in their
        session (per actor); returns the number written. Inferred steps get their session
        here (`_inferred_sessions`).

        The cap is the session row's `steps`, read and raised under its row lock in this
        transaction, so concurrent writers never pass it. Rows are locked in a fixed order
        (session rows, then summaries, each sorted), so concurrent batches do not deadlock."""
        if not steps:
            return 0
        async with rls_scope(self._session, bypass=True):
            resolved = await self._inferred_sessions(steps, inferred_idle_s)
            by_session: dict[tuple[str, str], list[int]] = defaultdict(list)
            for index, step in enumerate(resolved):
                by_session[(step.actor, step.session_id)].append(index)
            kept: set[int] = set()
            for actor, session_id in sorted(by_session):
                indexes = by_session[(actor, session_id)]
                keys = {"actor": actor, "session_id": session_id}
                held = (
                    await self._session.execute(
                        _HOLD_SESSION, {**keys, "at": resolved[indexes[0]].at}
                    )
                ).scalar_one()
                allowed = indexes[: max(0, max_per_session - int(held))]
                if not allowed:
                    continue
                await self._session.execute(
                    _GROW_SESSION,
                    {**keys, "n": len(allowed), "at": max(resolved[i].at for i in allowed)},
                )
                kept.update(allowed)
            written = [step for index, step in enumerate(resolved) if index in kept]
            if not written:
                return 0
            await self._session.execute(
                insert(AgentTraceStep),
                [{name: getattr(step, name) for name in _FIELDS} for step in written],
            )
            tallies: dict[_DomainKey, _DomainTally] = {}
            for step in written:
                key = (step.actor, step.session_id, step.domain_id)
                tallies.setdefault(key, _DomainTally(step.at, step.at)).add(step)
            for key in sorted(tallies, key=_domain_order):
                actor, session_id, domain_id = key
                tally = tallies[key]
                await self._session.execute(
                    _ADD_TO_DOMAIN,
                    {
                        "actor": actor,
                        "session_id": session_id,
                        "domain_id": domain_id,
                        "started_at": tally.started_at,
                        "last_at": tally.last_at,
                        "calls": tally.calls,
                        "notes": tally.notes,
                        "inferred": tally.inferred,
                        "tools": sorted(tally.tools),
                        "proposals": sorted(tally.proposals),
                    },
                )
        return len(written)

    async def _inferred_sessions(
        self, steps: Sequence[NewTraceStep], idle_s: float
    ) -> list[NewTraceStep]:
        """`steps` with the session of each inferred one: the session of the inferred step of
        the same actor and fingerprint nearest in time, at most `idle_s` before or after it
        (stored, or earlier in the batch), or a new session id.

        Steps are queued as their requests finish but carry the time their request started,
        so a client sending requests concurrently produces steps out of time order, within a
        batch and across batches; the window is looked at on both sides, and the batch is
        resolved in time order. Serialized per actor and fingerprint (locks taken in sorted
        order), so Keepers writing at the same time agree."""
        clients = sorted({(s.actor, s.fingerprint or "") for s in steps if s.inferred})
        for actor, fingerprint in clients:
            await self._session.execute(_INFERRED_LOCK, {"k": f"trace\x1f{actor}\x1f{fingerprint}"})
        idle = timedelta(seconds=idle_s)
        latest: dict[tuple[str, str | None], tuple[str, datetime]] = {}
        out = list(steps)
        order = sorted(
            (i for i, step in enumerate(steps) if step.inferred), key=lambda i: steps[i].at
        )
        for index in order:
            step = steps[index]
            client = (step.actor, step.fingerprint)
            candidates = [await self._stored_inferred_session(step, idle)]
            seen = latest.get(client)
            if seen is not None and abs(step.at - seen[1]) <= idle:
                candidates.append(seen)
            found = min(
                (c for c in candidates if c is not None),
                key=lambda c: abs(step.at - c[1]),
                default=None,
            )
            session_id = found[0] if found else f"{INFERRED_PREFIX}{uuid.uuid4().hex[:16]}"
            latest[client] = (session_id, step.at)
            out[index] = replace(step, session_id=session_id)
        return out

    async def _stored_inferred_session(
        self, step: NewTraceStep, idle: timedelta
    ) -> tuple[str, datetime] | None:
        """The session and time of the stored inferred step of the client of `step` nearest
        to it in time, within `idle` on either side; None when there is none."""
        t = AgentTraceStep
        found = (
            await self._session.execute(
                select(t.session_id, t.at)
                .where(
                    t.inferred,
                    t.actor == step.actor,
                    t.fingerprint == step.fingerprint,
                    t.at >= step.at - idle,
                    t.at <= step.at + idle,
                )
                .order_by(func.abs(func.extract("epoch", t.at - step.at)), t.seq.desc())
                .limit(1)
            )
        ).first()
        return None if found is None else (found.session_id, found.at)

    async def prune(self, before: datetime, *, limit: int) -> int:
        """Delete up to `limit` steps older than `before`, lower the session rows by what was
        deleted and recompute the summaries from the steps left (a row left without steps
        goes); the number deleted."""
        t = AgentTraceStep
        oldest = select(t.seq).where(t.at < before).order_by(t.at).limit(limit)
        async with rls_scope(self._session, bypass=True):
            deleted = (
                await self._session.execute(
                    delete(t)
                    .where(t.seq.in_(oldest))
                    .returning(t.actor, t.session_id, t.domain_id, t.type)
                )
            ).all()
            per_session: Counter[tuple[str, str]] = Counter()
            per_domain: dict[_DomainKey, Counter[str]] = defaultdict(Counter)
            for actor, session_id, domain_id, kind in deleted:
                per_session[(actor, session_id)] += 1
                per_domain[(actor, session_id, domain_id)][kind] += 1
            for (actor, session_id), count in sorted(per_session.items()):
                keys = {"actor": actor, "session_id": session_id}
                await self._session.execute(_SHRINK_SESSION, {**keys, "n": count})
                await self._session.execute(_DROP_SESSION, keys)
            for key in sorted(per_domain, key=_domain_order):
                actor, session_id, domain_id = key
                scope = {"actor": actor, "session_id": session_id, "domain_id": domain_id}
                counts = per_domain[key]
                await self._session.execute(_LOCK_DOMAIN, scope)
                await self._session.execute(
                    _SHRINK_DOMAIN, {**scope, "calls": counts["call"], "notes": counts["note"]}
                )
                await self._session.execute(_DROP_DOMAIN, scope)
        return len(deleted)

    async def sessions(
        self,
        visibility: TraceVisibility,
        *,
        limit: int,
        actor: str | None = None,
        domain_id: str | None = None,
        after: tuple[datetime, str, str] | None = None,
    ) -> list[TraceSessionSummary]:
        """Sessions with a visible step, most recent activity first, read from the per-Domain
        summaries (`agent_trace_session_domains`), not from the steps. `after` is the
        `(last_at, session_id, actor)` of the last session of the previous page."""
        d = AgentTraceSessionDomain
        conditions = [visibility.condition(d.actor, d.domain_id)]
        if actor is not None:
            conditions.append(d.actor == actor)
        if domain_id is not None:
            conditions.append(d.domain_id == domain_id)
        last_at = func.max(d.last_at)
        statement: Select[Any] = (
            select(
                d.session_id,
                d.actor,
                func.min(d.started_at).label("started_at"),
                last_at.label("last_at"),
                func.sum(d.calls).label("calls"),
                func.sum(d.notes).label("notes"),
                func.bool_or(d.inferred).label("inferred"),
                func.array_remove(func.array_agg(d.domain_id), None).label("domains"),
                func.jsonb_agg(d.tools).label("tools"),
                func.jsonb_agg(d.proposals).label("proposals"),
            )
            .where(*conditions)
            .group_by(d.session_id, d.actor)
            .order_by(last_at.desc(), d.session_id.desc(), d.actor.desc())
            .limit(limit)
        )
        if after is not None:
            statement = statement.having(tuple_(last_at, d.session_id, d.actor) < tuple_(*after))
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
                domains=tuple(sorted(set(row["domains"] or ()))),
                tools=_union(row["tools"]),
                proposals=_union(row["proposals"]),
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
