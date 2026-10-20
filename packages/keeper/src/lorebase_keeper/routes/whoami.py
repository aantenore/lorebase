"""Identify the caller."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from lorebase_keeper.auth.deps import PrincipalDep
from lorebase_keeper.errors import Problem
from lorebase_store.models.enums import PrincipalType

router = APIRouter(tags=["auth"])


class WhoAmI(BaseModel):
    """The authenticated caller."""

    id: str
    type: PrincipalType
    display_name: str
    method: str
    subjects: list[str]
    key_scopes: list[str]
    installation_admin: bool


@router.get(
    "/auth/whoami",
    summary="Identify the caller",
    responses={401: {"model": Problem, "description": "No valid credential."}},
)
async def whoami(principal: PrincipalDep) -> WhoAmI:
    """Who the credential of this request belongs to, with the ids grants may name."""
    return WhoAmI(
        id=principal.id,
        type=principal.type,
        display_name=principal.display_name,
        method=principal.method,
        subjects=list(principal.subjects),
        key_scopes=list(principal.key_scopes),
        installation_admin=principal.installation_admin,
    )
