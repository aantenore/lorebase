"""Agent trace API (docs/agent-trace.md): sessions, timelines and notes.

- `GET /trace/sessions`: sessions with a step the caller may see, most recent first;
- `GET /trace/sessions/{session_id}`: the timeline of one session, calls and notes in order;
- `POST /trace/sessions/{session_id}/notes`: attach a note (free text, such as an agent's
  reasoning between two tool calls) to a session of the caller.

Every principal sees its own steps. Steps of other principals are visible in the Domains where
the caller holds trace.read through a Domain-wide grant (the `admin` role holds it); notes
not tied to a Domain and steps outside a Domain only to installation administrators. Refs in
steps of other principals are filtered to the Kinds the caller may read in that Domain. Notes
are opaque text: stored as sent (bounded), never interpreted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_keeper.auth.audit import suppress
from lorebase_keeper.auth.deps import AuthDep, PrincipalDep, missing_permission
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.auth.rbac import DOMAIN_SCOPE_PREFIX
from lorebase_keeper.auth.runtime import AuthRuntime
from lorebase_keeper.db import Database
from lorebase_keeper.deps import KeeperSettingsDep
from lorebase_keeper.errors import NOT_FOUND, UNAVAILABLE, VALIDATION_FAILED, Problem, ProblemError
from lorebase_keeper.pagination import PageParamsDep, encode_cursor
from lorebase_keeper.tracing.config import SESSION_ID
from lorebase_keeper.tracing.recorder import TraceRecorder
from lorebase_store.tracing import (
    NewTraceStep,
    TraceRepository,
    TraceStepRecord,
    TraceVisibility,
)

router = APIRouter(prefix="/trace", tags=["trace"])

_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"model": Problem, "description": "No visible step of that session."},
    503: {"model": Problem, "description": "No database is configured."},
}

SessionId = Annotated[
    str,
    Path(
        min_length=1,
        max_length=128,
        pattern=SESSION_ID.pattern,
        description="Session id as sent in the session header.",
    ),
]


class TraceSessionOut(BaseModel):
    session_id: str
    actor: str = Field(description="Principal that made the calls.")
    started_at: datetime
    last_at: datetime
    calls: int
    notes: int
    domains: list[str]
    tools: list[str]
    proposals: list[str] = Field(description="Proposals the session opened or acted on.")
    inferred: bool = Field(
        default=False,
        description="Grouped heuristically: the calls carried no session id and were grouped by "
        "principal and client while they kept coming (keeper.trace.inferred_session_idle_s).",
    )


class TraceSessionPage(BaseModel):
    enabled: bool = Field(description="Whether new requests are traced (keeper.trace.enabled).")
    items: list[TraceSessionOut]
    next_cursor: str | None = None


class TraceRecordRef(BaseModel):
    kind: str
    key: str


class TraceLinkRef(BaseModel):
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    relation: str
    source: TraceRecordRef = Field(alias="from")
    to: TraceRecordRef


class TraceRefs(BaseModel):
    records: list[TraceRecordRef]
    links: list[TraceLinkRef]
    truncated: bool = Field(
        default=False, description="More refs were touched than kept or than visible."
    )


class TraceStepOut(BaseModel):
    step: int = Field(description="Position in the timeline, from 1.")
    seq: int
    type: Literal["call", "note"]
    at: datetime
    actor: str
    principal_type: str | None = None
    domain_id: str | None = None
    tool: str | None = None
    method: str | None = None
    route: str | None = Field(default=None, description="Route template, values unexpanded.")
    status: int | None = None
    latency_ms: float | None = None
    cache: str | None = Field(default=None, description="Response cache: hit, miss or bypass.")
    branch: str | None = None
    query: dict[str, Any] | None = Field(
        default=None,
        description="The query as sent; filter values kept only on fields that are not "
        "sensitive (keeper.trace.capture_filter_values).",
    )
    result: dict[str, Any] | None = Field(
        default=None, description="Counts, search mode and corrections, budget; no values."
    )
    refs: TraceRefs | None = None
    explain: dict[str, Any] | None = Field(
        default=None, description="Query plan summaries (`X-Lorebase-Trace: debug`)."
    )
    note: str | None = Field(default=None, description="Text of a note; opaque, not markup.")
    proposal_id: str | None = None
    request_id: str | None = None
    trace_id: str | None = Field(default=None, description="W3C trace id of the request.")
    inferred: bool = Field(
        default=False, description="The step had no session id; its session was inferred."
    )
    withheld: bool = Field(
        default=False,
        description="Another principal's step whose query, corrections and note text the "
        "reader may not see (it lacks Domain-wide data.read or a sensitivity clearance).",
    )


class TraceTimeline(BaseModel):
    session_id: str
    steps: list[TraceStepOut]
    truncated: bool = Field(description="The session has more visible steps than returned.")


class NoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(
        min_length=1, description="Free text; bounded by keeper.trace.max_note_chars."
    )
    domain_id: str | None = Field(
        default=None,
        max_length=256,
        description="Domain the note is about; principals holding trace.read there see it. "
        "Needs data.read in that Domain.",
    )
    step: int | None = Field(
        default=None, ge=0, description="The agent's own step counter, shown with the note."
    )


class NoteAccepted(BaseModel):
    recorded: bool = Field(description="False when tracing is disabled or the note was dropped.")


def _database(request: Request) -> Database:
    database = cast(Database | None, request.app.state.database)
    if database is None:
        raise ProblemError(UNAVAILABLE, "Agent traces need a configured database.")
    return database


def _sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    return _database(request).sessions


async def visibility(auth: AuthRuntime, principal: Principal) -> TraceVisibility:
    """What `principal` may see of the traces (see the module doc)."""
    domains = await auth.authorizer.domains_granting(principal, Permission.TRACE_READ)
    domainless = principal.method == "none" or (
        principal.installation_admin and not principal.key_scopes
    )
    owner_domains = (
        frozenset(
            s.removeprefix(DOMAIN_SCOPE_PREFIX)
            for s in principal.key_scopes
            if s.startswith(DOMAIN_SCOPE_PREFIX)
        )
        if principal.key_scopes
        else None
    )
    return TraceVisibility(
        owner=principal.id, domains=domains, domainless=domainless, owner_domains=owner_domains
    )


@router.get(
    "/sessions",
    summary="List agent trace sessions",
    response_model=TraceSessionPage,
    responses=_ERRORS,
)
async def list_sessions(
    request: Request,
    principal: PrincipalDep,
    auth: AuthDep,
    keeper: KeeperSettingsDep,
    page: PageParamsDep,
    actor: Annotated[str | None, Query(max_length=256, description="Only this principal.")] = None,
    domain_id: Annotated[
        str | None, Query(max_length=256, description="Only steps in this Domain.")
    ] = None,
) -> TraceSessionPage:
    """Sessions with a step the caller may see, most recent activity first."""
    after = None
    if page.position is not None:
        try:
            after = (
                datetime.fromisoformat(str(page.position["at"])),
                str(page.position["session"]),
                str(page.position["actor"]),
            )
        except (KeyError, ValueError):
            raise _invalid_cursor() from None
    seen = await visibility(auth, principal)
    async with _sessions(request)() as session:
        found = await TraceRepository(session).sessions(
            seen, limit=page.limit + 1, actor=actor, domain_id=domain_id, after=after
        )
    items = found[: page.limit]
    next_cursor = None
    if len(found) > page.limit:
        last = items[-1]
        next_cursor = encode_cursor(
            {"at": last.last_at.isoformat(), "session": last.session_id, "actor": last.actor}
        )
    return TraceSessionPage(
        enabled=keeper.trace.enabled,
        items=[
            TraceSessionOut(
                session_id=s.session_id,
                actor=s.actor,
                started_at=s.started_at,
                last_at=s.last_at,
                calls=s.calls,
                notes=s.notes,
                domains=list(s.domains),
                tools=list(s.tools),
                proposals=list(s.proposals),
                inferred=s.inferred,
            )
            for s in items
        ],
        next_cursor=next_cursor,
    )


def _invalid_cursor() -> ProblemError:
    return ProblemError(
        VALIDATION_FAILED,
        "One or more request members are invalid.",
        extensions={
            "errors": [{"pointer": "/query/cursor", "message": "is not valid", "code": "invalid"}]
        },
    )


CLEARANCES = (Permission.PII_READ, Permission.CONFIDENTIAL_READ, Permission.RESTRICTED_READ)


@dataclass(frozen=True)
class _View:
    query: dict[str, Any] | None
    result: dict[str, Any] | None
    refs: TraceRefs | None
    note: str | None
    explain: dict[str, Any] | None
    withheld: bool = False


class _RefFilter:
    """Kinds a reader may read per Domain, looked up once per Domain."""

    def __init__(self, auth: AuthRuntime, principal: Principal) -> None:
        self._auth = auth
        self._principal = principal
        self._readable: dict[str, frozenset[str] | None] = {}
        self._cleared: dict[str, bool] = {}
        self._explains: dict[str, bool] = {}

    async def kinds(self, domain_id: str) -> frozenset[str] | None:
        """None: every Kind."""
        if domain_id not in self._readable:
            authorizer = self._auth.authorizer
            if await authorizer.allows(self._principal, Permission.DATA_READ, domain_id):
                self._readable[domain_id] = None
            else:
                self._readable[domain_id] = await authorizer.granted_kinds(
                    self._principal, Permission.DATA_READ, domain_id
                )
        return self._readable[domain_id]

    async def cleared(self, domain_id: str) -> bool:
        """Whether the reader may see what another principal typed in the Domain (queries,
        search text and corrections, notes): Domain-wide data.read and every sensitivity
        clearance, since such text may hold values the reader would see masked."""
        if domain_id not in self._cleared:
            authorizer = self._auth.authorizer
            self._cleared[domain_id] = await self.kinds(domain_id) is None and all(
                [
                    await authorizer.allows(self._principal, permission, domain_id)
                    for permission in CLEARANCES
                ]
            )
        return self._cleared[domain_id]

    async def explains(self, domain_id: str) -> bool:
        if domain_id not in self._explains:
            self._explains[domain_id] = await self._auth.authorizer.allows(
                self._principal, Permission.TRACE_EXPLAIN, domain_id
            )
        return self._explains[domain_id]

    async def view(self, step: TraceStepRecord) -> _View:
        """A step as the reader may see it. Its own steps in full. Of another principal's
        step: refs of the Kinds the reader may read; query, corrections and note text only for
        a reader cleared in the Domain (`cleared`; outside a Domain, the installation
        administrators who alone see such steps); query plans only with trace.explain."""
        if step.actor == self._principal.id:
            return _View(step.query, step.result, self.refs(step, None), step.note, step.explain)
        if step.domain_id is None:
            return _View(step.query, step.result, self.refs(step, None), step.note, step.explain)
        allowed = await self.kinds(step.domain_id)
        refs = self.refs(step, allowed)
        explain = step.explain if await self.explains(step.domain_id) else None
        if await self.cleared(step.domain_id):
            return _View(step.query, step.result, refs, step.note, explain)
        # A reader limited to Kinds that are invisible to it (design 13.3) learns nothing of
        # the others: no counts, no query plans, no hint that refs were left out.
        invisible = allowed is not None and self._auth.authorizer.invisible_kinds
        kept = ("agent_step",) if invisible else ("counts", "agent_step")
        result = step.result
        if result is not None:
            result = {k: v for k, v in result.items() if k in kept}
        if invisible:
            explain = None
        note = None if step.note is None else ""
        return _View(None, result, refs, note, explain, withheld=True)

    def refs(self, step: TraceStepRecord, allowed: frozenset[str] | None) -> TraceRefs | None:
        stored = step.refs
        if not stored:
            return None
        records = [
            TraceRecordRef(kind=r[0], key=r[1])
            for r in stored.get("records") or ()
            if allowed is None or r[0] in allowed
        ]
        links = [
            TraceLinkRef(
                relation=link[0],
                source=TraceRecordRef(kind=link[1], key=link[2]),
                to=TraceRecordRef(kind=link[3], key=link[4]),
            )
            for link in stored.get("links") or ()
            if allowed is None or (link[1] in allowed and link[3] in allowed)
        ]
        hidden = len(records) + len(links) < len(stored.get("records") or ()) + len(
            stored.get("links") or ()
        )
        truncated = bool(stored.get("truncated")) or hidden
        if allowed is not None and self._auth.authorizer.invisible_kinds:
            # Whether refs were left out, or cut by the recorder's cap (which counts every
            # Kind), would tell about Kinds invisible to the reader.
            truncated = False
        return TraceRefs(records=records, links=links, truncated=truncated)


@router.get(
    "/sessions/{session_id}",
    summary="Timeline of an agent trace session",
    response_model=TraceTimeline,
    responses=_ERRORS,
)
async def get_session(
    session_id: SessionId,
    request: Request,
    principal: PrincipalDep,
    auth: AuthDep,
    keeper: KeeperSettingsDep,
    actor: Annotated[
        str | None,
        Query(
            max_length=256,
            description="Principal whose steps are read; default: the caller. A session id "
            "names one session per principal.",
        ),
    ] = None,
) -> TraceTimeline:
    """Calls and notes of one principal's session in order, as far as the caller may see
    them."""
    actor = actor or principal.id
    seen = await visibility(auth, principal)
    limit = keeper.trace.max_steps_per_session
    async with _sessions(request)() as session:
        steps = await TraceRepository(session).steps(session_id, seen, limit=limit + 1, actor=actor)
    if not steps:
        raise ProblemError(NOT_FOUND, f"Agent trace session {session_id!r} not found.")
    refs = _RefFilter(auth, principal)
    out = []
    for position, step in enumerate(steps[:limit], start=1):
        view = await refs.view(step)
        out.append(
            TraceStepOut(
                step=position,
                seq=step.seq,
                type=cast(Literal["call", "note"], step.type),
                at=step.at,
                actor=step.actor,
                principal_type=step.principal_type,
                domain_id=step.domain_id,
                tool=step.tool,
                method=step.method,
                route=step.route,
                status=step.status,
                latency_ms=step.latency_ms,
                cache=step.cache,
                branch=step.branch,
                query=view.query,
                result=view.result,
                refs=view.refs,
                explain=view.explain,
                note=view.note,
                withheld=view.withheld,
                proposal_id=step.proposal_id,
                request_id=step.request_id,
                trace_id=step.trace_id,
                inferred=step.inferred,
            )
        )
    return TraceTimeline(session_id=session_id, steps=out, truncated=len(steps) > limit)


@router.post(
    "/sessions/{session_id}/notes",
    status_code=202,
    summary="Attach a note to an agent trace session",
    response_model=NoteAccepted,
    responses={
        **_ERRORS,
        422: {"model": Problem, "description": "Empty or too long note, or bad session id."},
    },
)
async def add_note(
    session_id: SessionId,
    body: NoteIn,
    request: Request,
    principal: PrincipalDep,
    auth: AuthDep,
    keeper: KeeperSettingsDep,
) -> NoteAccepted:
    """Queue a note of the caller's session (written off the request path, like calls)."""
    suppress(request)  # debugging data, not an action on the Domain: kept out of the audit log
    settings = keeper.trace
    if len(body.text) > settings.max_note_chars:
        raise ProblemError(
            VALIDATION_FAILED,
            "One or more request members are invalid.",
            extensions={
                "errors": [
                    {
                        "pointer": "/body/text",
                        "message": f"must not exceed {settings.max_note_chars} characters",
                        "code": "too_long",
                    }
                ]
            },
        )
    if body.domain_id is not None and not await auth.authorizer.allows_anywhere(
        principal, Permission.DATA_READ, body.domain_id
    ):
        raise missing_permission(Permission.DATA_READ, body.domain_id)
    recorder = cast(TraceRecorder | None, request.app.state.trace)
    if recorder is None:
        return NoteAccepted(recorded=False)
    recorded = recorder.offer(
        NewTraceStep(
            session_id=session_id,
            actor=principal.id,
            type="note",
            at=datetime.now(UTC),
            principal_type=principal.type.value,
            domain_id=body.domain_id,
            note=body.text,
            result={"agent_step": body.step} if body.step is not None else None,
        )
    )
    return NoteAccepted(recorded=recorded)
