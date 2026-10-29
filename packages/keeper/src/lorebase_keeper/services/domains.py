"""Domain use cases: create (root Revision and default branch), read, update, delete."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError

from lorebase_core.model import Domain
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.context import StoreAccess, author_of, require_domain
from lorebase_keeper.errors import NOT_FOUND, ProblemError
from lorebase_keeper.services.config import DomainSettings
from lorebase_keeper.services.errors import ALREADY_EXISTS
from lorebase_store.auth import GrantRepository
from lorebase_store.commit import create_domain
from lorebase_store.repositories import DomainRepository, DomainRow, RefRepository


@dataclass(frozen=True)
class CreatedDomain:
    """A Domain and the head of its default branch; `replayed` when it already existed."""

    domain: DomainRow
    head: bytes
    replayed: bool = False


async def create(
    store: StoreAccess,
    domain: Domain,
    principal: Principal,
    settings: DomainSettings,
    *,
    idempotency_key: str | None,
) -> CreatedDomain:
    """Create a Domain: row, root Revision, protected default branch and the creator's grant.

    With an idempotency key, creating a Domain that the same principal already created
    returns it (`replayed`); otherwise an existing id is a conflict.
    """
    async with store.sessions() as session:
        existing = await DomainRepository(session).get(domain.id)
    if existing is not None:
        return await _existing(store, existing, principal, idempotency_key)
    try:
        async with store.sessions() as session, session.begin():
            result = await create_domain(
                session,
                domain,
                author=author_of(principal),
                leaf_size=settings.leaf_size,
                runtime=store.runtime,
                clock=store.clock,
            )
            if not settings.protect_default_branch:
                await RefRepository(session, domain.id).set_protected(domain.default_branch, False)
            if settings.creator_role and principal.method != "none":
                await GrantRepository(session).add(
                    domain_id=domain.id,
                    subject=principal.id,
                    role=settings.creator_role,
                    created_by=principal.id,
                )
            row = await DomainRepository(session).require(domain.id)
    except IntegrityError:
        async with store.sessions() as session:
            found = await DomainRepository(session).get(domain.id)
        if found is None:
            raise
        return await _existing(store, found, principal, idempotency_key)
    return CreatedDomain(domain=row, head=result.revision_id)


async def _existing(
    store: StoreAccess, row: DomainRow, principal: Principal, idempotency_key: str | None
) -> CreatedDomain:
    if idempotency_key is None or row.created_by != principal.id:
        raise ProblemError(ALREADY_EXISTS, f"Domain {row.id!r} already exists.")
    async with store.sessions() as session, session.begin():
        await require_domain(session, row.id)
        ref = await RefRepository(session, row.id).require(row.default_branch)
    return CreatedDomain(domain=row, head=ref.head, replayed=True)


async def get(store: StoreAccess, domain_id: str) -> tuple[DomainRow, bytes | None]:
    """The Domain and the head of its default branch."""
    async with store.sessions() as session, session.begin():
        domain = await require_domain(session, domain_id)
        ref = await RefRepository(session, domain_id).get(domain.default_branch)
    return domain, None if ref is None else ref.head


async def page(store: StoreAccess, *, limit: int, after: str | None) -> list[DomainRow]:
    """Domains ordered by id."""
    async with store.sessions() as session:
        return await DomainRepository(session).list(limit=limit, after=after)


@dataclass(frozen=True)
class SettingChange:
    """A Domain setting member changed by an update: its value before and after (`None`:
    absent)."""

    member: str
    before: Any
    after: Any


async def update(
    store: StoreAccess,
    domain_id: str,
    *,
    display_name: str | None,
    description: str | None,
    settings: Mapping[str, Any] | None = None,
) -> tuple[DomainRow, list[SettingChange]]:
    """Change display metadata (not versioned: the Domain element of the schema is unchanged)
    and replace the members of `settings` given (a `None` value removes the member). Returns
    the row and the setting members that changed."""
    changes: list[SettingChange] = []
    async with store.sessions() as session, session.begin():
        current = await require_domain(session, domain_id)
        merged: dict[str, Any] | None = None
        if settings:
            merged = dict(current.settings or {})
            for member, value in settings.items():
                before = merged.get(member)
                if value is None:
                    merged.pop(member, None)
                else:
                    merged[member] = value
                if before != value:
                    changes.append(SettingChange(member, before, value))
        row = await DomainRepository(session).update(
            domain_id,
            display_name=display_name,
            description=description,
            settings=merged if changes else None,
        )
    if row is None:  # pragma: no cover - deleted concurrently
        raise ProblemError(NOT_FOUND, f"Domain {domain_id!r} not found.")
    return row, changes


async def delete(store: StoreAccess, domain_id: str) -> None:
    """Delete a Domain and everything scoped to it. The audit log keeps its entries."""
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        await DomainRepository(session).delete(domain_id)
