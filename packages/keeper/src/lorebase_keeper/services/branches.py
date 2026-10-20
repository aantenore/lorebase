"""Branch and tag use cases (design 6.5): create, read, delete, protect."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError

from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.context import (
    StoreAccess,
    require_branch,
    require_domain,
    require_revision,
)
from lorebase_keeper.errors import NOT_FOUND, VALIDATION_FAILED, ProblemError
from lorebase_keeper.services.config import BranchSettings
from lorebase_keeper.services.errors import (
    ALREADY_EXISTS,
    BRANCH_BUSY,
    LIMIT_EXCEEDED,
    PROTECTED_BRANCH,
)
from lorebase_store.materialize import BranchExistsError, MaterializationMode, create_branch
from lorebase_store.models.enums import Materialization, RefKind
from lorebase_store.repositories import (
    LockTimeoutError,
    RefRepository,
    RefRow,
    RevisionRepository,
)

TAG_CREATE_REASON = "tag.create"


@dataclass(frozen=True)
class BranchState:
    """A branch with its position relative to the default branch."""

    ref: RefRow
    default: bool
    ahead: int
    behind: int


@dataclass(frozen=True)
class CreatedRef:
    """A new branch or tag; `mode` tells how a branch got its rows, `replayed` that it existed."""

    ref: RefRow
    mode: MaterializationMode | None = None
    replayed: bool = False
    default: bool = False


def check_ref_name(name: str, settings: BranchSettings, pointer: str = "/body/name") -> None:
    """422 unless `name` is a valid branch or tag name."""
    if len(name) > settings.max_name_length or not re.fullmatch(settings.name_pattern, name):
        raise ProblemError(
            VALIDATION_FAILED,
            "One or more request members are invalid.",
            extensions={
                "errors": [
                    {
                        "pointer": pointer,
                        "message": f"must match {settings.name_pattern} and be at most "
                        f"{settings.max_name_length} characters",
                        "code": "invalid",
                    }
                ]
            },
        )


def _busy(settings: BranchSettings) -> ProblemError:
    return ProblemError(
        BRANCH_BUSY,
        "Another writer holds the branch; retry later.",
        headers={"Retry-After": str(settings.lock_retry_after_s)},
    )


async def page(
    store: StoreAccess, domain_id: str, kind: RefKind, *, limit: int, after: str | None
) -> tuple[str, list[RefRow]]:
    """Refs of one kind ordered by name, with the Domain's default branch name."""
    async with store.sessions() as session, session.begin():
        domain = await require_domain(session, domain_id)
        refs = await RefRepository(session, domain_id).page(kind, limit=limit, after=after)
    return domain.default_branch, refs


async def get(store: StoreAccess, domain_id: str, name: str) -> BranchState:
    """A branch with ahead and behind counts against the default branch head."""
    async with store.sessions() as session, session.begin():
        domain = await require_domain(session, domain_id)
        ref = await require_branch(session, domain_id, name)
        default = await RefRepository(session, domain_id).get(domain.default_branch)
        ahead = behind = 0
        if default is not None and default.name != ref.name:
            ahead, behind = await RevisionRepository(session, domain_id).ahead_behind(
                ref.head, default.head
            )
    return BranchState(
        ref=ref, default=ref.name == domain.default_branch, ahead=ahead, behind=behind
    )


async def create(
    store: StoreAccess,
    domain_id: str,
    name: str,
    principal: Principal,
    settings: BranchSettings,
    *,
    source: str | None,
    revision: bytes | None,
    idempotency_key: str | None,
) -> CreatedRef:
    """Create a fully materialized branch at `revision` or at the head of `source`.

    `source` is a branch or tag (default: the Domain's default branch). Rows are copied from
    the source branch when the new branch starts at its head, rebuilt from the tree otherwise.
    """
    check_ref_name(name, settings)
    try:
        async with store.sessions() as session, session.begin():
            domain = await require_domain(session, domain_id)
            refs = RefRepository(session, domain_id)
            existing = await refs.get(name)
            if existing is not None:
                return _replay(
                    existing,
                    principal,
                    idempotency_key,
                    RefKind.BRANCH,
                    default=name == domain.default_branch,
                )
            source_name = source or domain.default_branch
            if await refs.get(source_name) is None:
                raise ProblemError(NOT_FOUND, f"Branch or tag {source_name!r} not found.")
            if revision is not None:
                await require_revision(session, domain_id, revision)
            materialized = await refs.count(RefKind.BRANCH, Materialization.FULL)
            if materialized >= settings.max_materialized_per_domain:
                raise ProblemError(
                    LIMIT_EXCEEDED,
                    f"Domain {domain_id!r} already has {materialized} materialized branches "
                    f"(limit {settings.max_materialized_per_domain}); delete one first.",
                )
            created = await create_branch(
                session,
                domain_id,
                name,
                created_by=principal.id,
                source=source_name,
                start=revision,
                runtime=store.runtime,
                clock=store.clock,
            )
    except BranchExistsError:
        async with store.sessions() as session, session.begin():
            await require_domain(session, domain_id)
            found = await RefRepository(session, domain_id).require(name)
        return _replay(found, principal, idempotency_key, RefKind.BRANCH)
    except LockTimeoutError:
        raise _busy(settings) from None
    return CreatedRef(ref=created.ref, mode=created.mode)


def _replay(
    existing: RefRow,
    principal: Principal,
    idempotency_key: str | None,
    kind: RefKind,
    *,
    default: bool = False,
) -> CreatedRef:
    """An existing ref returned for a retried create, or 409."""
    if idempotency_key is None or existing.created_by != principal.id or existing.kind is not kind:
        raise ProblemError(ALREADY_EXISTS, f"A branch or tag named {existing.name!r} exists.")
    return CreatedRef(ref=existing, replayed=True, default=default)


async def delete(store: StoreAccess, domain_id: str, name: str, settings: BranchSettings) -> RefRow:
    """Delete a branch and its materialized rows. The default and protected branches stay."""
    try:
        async with store.sessions() as session, session.begin():
            domain = await require_domain(session, domain_id)
            refs = RefRepository(session, domain_id)
            await refs.lock(name, timeout_ms=store.runtime.settings.commit.lock_timeout_ms)
            ref = await require_branch(session, domain_id, name)
            if name == domain.default_branch:
                raise ProblemError(
                    PROTECTED_BRANCH, f"{name!r} is the default branch of the Domain."
                )
            if ref.protected:
                raise ProblemError(
                    PROTECTED_BRANCH, f"Branch {name!r} is protected; unprotect it first."
                )
            await refs.delete(name)
    except LockTimeoutError:
        raise _busy(settings) from None
    return ref


async def set_protected(
    store: StoreAccess, domain_id: str, name: str, protected: bool
) -> BranchState:
    """Protect or unprotect a branch."""
    async with store.sessions() as session, session.begin():
        domain = await require_domain(session, domain_id)
        await require_branch(session, domain_id, name)
        ref = await RefRepository(session, domain_id).set_protected(name, protected)
    assert ref is not None
    return BranchState(ref=ref, default=name == domain.default_branch, ahead=0, behind=0)


async def create_tag(
    store: StoreAccess,
    domain_id: str,
    name: str,
    principal: Principal,
    settings: BranchSettings,
    *,
    source: str | None,
    revision: bytes | None,
    idempotency_key: str | None,
) -> CreatedRef:
    """Create an immutable tag at `revision` or at the head of `source` (default branch)."""
    check_ref_name(name, settings)
    try:
        return await _create_tag(
            store, domain_id, name, principal, source, revision, idempotency_key
        )
    except IntegrityError:
        raise ProblemError(ALREADY_EXISTS, f"A branch or tag named {name!r} exists.") from None


async def _create_tag(
    store: StoreAccess,
    domain_id: str,
    name: str,
    principal: Principal,
    source: str | None,
    revision: bytes | None,
    idempotency_key: str | None,
) -> CreatedRef:
    async with store.sessions() as session, session.begin():
        domain = await require_domain(session, domain_id)
        refs = RefRepository(session, domain_id)
        existing = await refs.get(name)
        if existing is not None:
            return _replay(existing, principal, idempotency_key, RefKind.TAG)
        if revision is None:
            source_ref = await refs.get(source or domain.default_branch)
            if source_ref is None:
                raise ProblemError(NOT_FOUND, f"Branch or tag {source!r} not found.")
            revision = source_ref.head
        await require_revision(session, domain_id, revision)
        ref = await refs.create(
            name,
            revision,
            created_at=store.clock(),
            created_by=principal.id,
            kind=RefKind.TAG,
            materialization=Materialization.NONE,
            reason=TAG_CREATE_REASON,
        )
    return CreatedRef(ref=ref)
