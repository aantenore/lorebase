"""Principals and groups (design 10.2, 13.2).

Users come from an identity provider and are provisioned on first sign-in; service principals
are created by administrators and own API keys. Ids are prefixed: `usr_`, `svc_`.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import CompoundSelect, Select, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.auth.scope import new_id
from lorebase_store.models import Group, GroupMember, Principal
from lorebase_store.models.enums import PrincipalType

ID_PREFIXES = {
    PrincipalType.USER: "usr",
    PrincipalType.SERVICE: "svc",
    PrincipalType.API_KEY: "key",
}


@dataclass(frozen=True)
class PrincipalRecord:
    """A stored principal."""

    id: str
    type: PrincipalType
    display_name: str
    external_subject: str | None
    created_at: datetime


def _record(row: Principal) -> PrincipalRecord:
    return PrincipalRecord(
        id=row.id,
        type=row.type,
        display_name=row.display_name,
        external_subject=row.external_subject,
        created_at=row.created_at,
    )


class PrincipalRepository:
    """Principals of one installation (no row-level security: the table is installation-wide)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, principal_id: str) -> PrincipalRecord | None:
        """The principal with this id, if any."""
        row = await self._session.get(Principal, principal_id)
        return _record(row) if row else None

    async def get_by_subject(self, external_subject: str) -> PrincipalRecord | None:
        """The principal linked to an identity provider subject, if any."""
        row = (
            await self._session.execute(
                select(Principal).where(Principal.external_subject == external_subject)
            )
        ).scalar_one_or_none()
        return _record(row) if row else None

    async def add(
        self,
        type_: PrincipalType,
        display_name: str,
        *,
        external_subject: str | None = None,
        principal_id: str | None = None,
    ) -> PrincipalRecord:
        """Insert a principal. A taken `external_subject` raises `IntegrityError`."""
        row = Principal(
            id=principal_id or new_id(ID_PREFIXES[type_]),
            type=type_,
            display_name=display_name,
            external_subject=external_subject,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _record(row)

    async def get_or_create_user(
        self, external_subject: str, display_name: str
    ) -> tuple[PrincipalRecord, bool]:
        """The user of a provider subject, created on first use. Safe under concurrency.

        Returns the principal and whether this call created it.
        """
        statement = (
            insert(Principal)
            .values(
                id=new_id(ID_PREFIXES[PrincipalType.USER]),
                type=PrincipalType.USER,
                display_name=display_name,
                external_subject=external_subject,
            )
            .on_conflict_do_nothing(
                index_elements=[Principal.external_subject],
                index_where=Principal.external_subject.is_not(None),
            )
            .returning(Principal.id)
        )
        created = (await self._session.execute(statement)).scalar_one_or_none() is not None
        existing = await self.get_by_subject(external_subject)
        if existing is None:  # pragma: no cover - the subject row exists after the upsert
            raise RuntimeError("principal vanished after upsert")
        return existing, created

    async def list(
        self,
        *,
        type_: PrincipalType | None = None,
        limit: int,
        after: tuple[datetime, str] | None = None,
    ) -> list[PrincipalRecord]:
        """Principals in creation order; `after` is the `(created_at, id)` of the last item."""
        statement = select(Principal).order_by(Principal.created_at, Principal.id).limit(limit)
        if type_ is not None:
            statement = statement.where(Principal.type == type_)
        if after is not None:
            statement = statement.where(tuple_(Principal.created_at, Principal.id) > after)
        return [_record(row) for row in (await self._session.execute(statement)).scalars()]


class GroupRepository:
    """Groups and memberships."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def group_ids_for(
        self, principal_id: str, external_groups: Collection[str] = ()
    ) -> list[str]:
        """Ids of the groups a principal belongs to.

        Groups are found by stored membership and by the provider group names carried in the
        principal's current token (`Group.external_group`).
        """
        by_membership = select(GroupMember.group_id).where(GroupMember.principal_id == principal_id)
        statement: Select[Any] | CompoundSelect[Any] = by_membership
        if external_groups:
            statement = by_membership.union(
                select(Group.id).where(Group.external_group.in_(list(external_groups)))
            )
        return sorted(str(item) for item in (await self._session.execute(statement)).scalars())

    async def subject_exists(self, subject_id: str) -> bool:
        """Whether `subject_id` names a principal or a group (grant subjects)."""
        principal = await self._session.get(Principal, subject_id)
        return principal is not None or await self._session.get(Group, subject_id) is not None
