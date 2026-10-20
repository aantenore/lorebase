"""Request context of the Domain-scoped write endpoints: store access, Domain, branch, Revision.

`StoreDep` gives a route the session factory, the store runtime (settings and node cache) and
the clock. The resolution helpers run inside a caller's session and answer 404 problems for
unknown Domains, branches and Revisions.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, cast

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.canonical import from_hex
from lorebase_core.revision import Author
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.auth.runtime import AuthRuntime
from lorebase_keeper.db import Database
from lorebase_keeper.errors import NOT_FOUND, UNAVAILABLE, VALIDATION_FAILED, ProblemError
from lorebase_platform.settings import Settings
from lorebase_store.models.enums import RefKind
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
