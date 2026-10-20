"""Request context of the Domain-scoped write endpoints: store access, Domain, branch, Revision.

`StoreDep` gives a route the session factory, the store runtime (settings and node cache) and
the clock. `ReadSessionDep` gives a read endpoint a session on a read replica when
`keeper.reads.prefer_replica` is set and a replica is fresh enough, otherwise on the primary.
The resolution helpers run inside a caller's session and answer 404 problems for
unknown Domains, branches and Revisions.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, cast

from fastapi import Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.canonical import from_hex
from lorebase_core.revision import Author
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.auth.runtime import AuthRuntime
from lorebase_keeper.db import Database
from lorebase_keeper.errors import NOT_FOUND, UNAVAILABLE, VALIDATION_FAILED, ProblemError
from lorebase_keeper.settings import KeeperSettings
from lorebase_platform.settings import Settings
from lorebase_store.models.enums import RefKind
from lorebase_store.replicas import (
    ReadSession,
    ReadSessionFactory,
    ReplicaUnavailableError,
)
from lorebase_store.repositories import (
    DomainRepository,
    DomainRow,
    RefRepository,
    RefRow,
    RevisionRepository,
    RevisionRow,
    scope_to_domain,
)
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import store_settings

_RUNTIME_STATE = "store_runtime"
_REVISION_ID = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class StoreAccess:
    """What the write endpoints need from persistence."""

    sessions: async_sessionmaker[AsyncSession]
    runtime: StoreRuntime
    clock: Callable[[], datetime]


def get_store(request: Request) -> StoreAccess:
    """Store access of the application; 503 when no database is configured."""
    state = request.app.state
    database = cast(Database | None, getattr(state, "database", None))
    if database is None:
        raise ProblemError(UNAVAILABLE, "This operation needs a configured database.")
    runtime = cast(StoreRuntime | None, getattr(state, _RUNTIME_STATE, None))
    if runtime is None:
        runtime = StoreRuntime.from_settings(store_settings(cast(Settings, state.settings)))
        setattr(state, _RUNTIME_STATE, runtime)
    auth = cast(AuthRuntime, state.auth)
    return StoreAccess(sessions=database.sessions, runtime=runtime, clock=auth.clock)


StoreDep = Annotated[StoreAccess, Depends(get_store)]


@dataclass(frozen=True)
class ReadAccess:
    """Opens read sessions according to `keeper.reads` and `database.read_*`."""

    reads: ReadSessionFactory
    prefer_replica: bool

    def open(
        self,
        *,
        domain_id: str | None = None,
        branch: str | None = None,
        expected_head: bytes | None = None,
        read_your_writes: bool = False,
    ) -> AbstractAsyncContextManager[ReadSession]:
        """A read session (see `ReadSessionFactory.open`); 503 when the replicas cannot serve
        the read and falling back to the primary is disabled."""
        return _guarded(
            self.reads,
            domain_id=domain_id,
            branch=branch,
            expected_head=expected_head,
            read_your_writes=read_your_writes,
            prefer_replica=self.prefer_replica,
        )


@asynccontextmanager
async def _guarded(reads: ReadSessionFactory, **options: Any) -> AsyncIterator[ReadSession]:
    try:
        async with reads.open(**options) as read:
            yield read
    except ReplicaUnavailableError as exc:
        raise ProblemError(
            UNAVAILABLE, "No read replica is fresh enough to serve this read."
        ) from exc


def get_read_access(request: Request) -> ReadAccess:
    """Read routing of the application; 503 when no database is configured."""
    state = request.app.state
    database = cast(Database | None, getattr(state, "database", None))
    if database is None:
        raise ProblemError(UNAVAILABLE, "This operation needs a configured database.")
    reads = database.reads or ReadSessionFactory(database.sessions)
    keeper = cast(KeeperSettings, state.keeper_settings)
    return ReadAccess(reads=reads, prefer_replica=keeper.reads.prefer_replica)


ReadAccessDep = Annotated[ReadAccess, Depends(get_read_access)]


async def read_session(
    request: Request,
    access: ReadAccessDep,
    min_revision: Annotated[
        str | None,
        Query(
            pattern=_REVISION_ID.pattern,
            description="Revision id the branch head must be at on the serving replica "
            "(read-your-writes); a replica that has not replayed it is skipped, so the answer "
            "may come from the primary.",
        ),
    ] = None,
) -> AsyncIterator[ReadSession]:
    """Session for a read endpoint, opened for the `domain_id` of the path.

    The branch is the `branch` path parameter or query parameter, else the Domain's default.
    The session has an open transaction scoped to the Domain and is closed after the response.
    """
    params = request.path_params
    branch = params.get("branch") or request.query_params.get("branch")
    expected = parse_revision_id(min_revision, "min_revision") if min_revision else None
    async with access.open(
        domain_id=params.get("domain_id"), branch=branch, expected_head=expected
    ) as read:
        yield read


ReadSessionDep = Annotated[ReadSession, Depends(read_session)]


def author_of(principal: Principal) -> Author:
    """Revision author of a request."""
    return Author(principal=principal.id)


async def require_domain(session: AsyncSession, domain_id: str) -> DomainRow:
    """The Domain row, scoping the transaction to it; 404 when it does not exist."""
    domain = await DomainRepository(session).get(domain_id)
    if domain is None:
        raise ProblemError(NOT_FOUND, f"Domain {domain_id!r} not found.")
    await scope_to_domain(session, domain_id)
    return domain


async def require_branch(session: AsyncSession, domain_id: str, name: str) -> RefRow:
    """A branch of the Domain; 404 for unknown names and for tags."""
    ref = await RefRepository(session, domain_id).get(name)
    if ref is None or ref.kind is not RefKind.BRANCH:
        raise ProblemError(NOT_FOUND, f"Branch {name!r} not found.")
    return ref


def parse_revision_id(text: str, pointer: str) -> bytes:
    """A Revision id given as 64 lowercase hex characters; 422 otherwise."""
    if not _REVISION_ID.match(text):
        raise ProblemError(
            VALIDATION_FAILED,
            "One or more request members are invalid.",
            extensions={
                "errors": [
                    {"pointer": pointer, "message": "is not a Revision id", "code": "invalid"}
                ]
            },
        )
    return from_hex(text)


async def require_revision(session: AsyncSession, domain_id: str, revision: bytes) -> RevisionRow:
    """A Revision of the Domain; 404 when unknown."""
    found = await RevisionRepository(session, domain_id).get(revision)
    if found is None:
        raise ProblemError(NOT_FOUND, f"Revision {revision.hex()} not found.")
    return found


@dataclass(frozen=True)
class ResolvedRevision:
    """A Revision selected by branch, tag or id; `ref` is the ref it was read through."""

    revision: RevisionRow
    ref: RefRow | None


async def resolve_revision(
    session: AsyncSession, domain: DomainRow, *, branch: str | None, at: str | None
) -> ResolvedRevision:
    """`at` (Revision id or tag/branch name) when given, otherwise the head of `branch`
    (default: the Domain's default branch)."""
    refs = RefRepository(session, domain.id)
    if at is not None:
        if _REVISION_ID.match(at):
            return ResolvedRevision(await require_revision(session, domain.id, from_hex(at)), None)
        ref = await refs.get(at)
        if ref is None:
            raise ProblemError(NOT_FOUND, f"No Revision, tag or branch named {at!r}.")
        return ResolvedRevision(await require_revision(session, domain.id, ref.head), ref)
    ref = await require_branch(session, domain.id, branch or domain.default_branch)
    return ResolvedRevision(await require_revision(session, domain.id, ref.head), ref)
