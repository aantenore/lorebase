"""Branches and tags (design 6.5, 11.1).

Branch names are hierarchical (`agent/bot-1/task`); in paths they are written as they are or
URL-encoded. Grants narrowed to a branch pattern apply to the branch named in the path or,
on creation, in the body.

| Route | Permission |
|-------|------------|
| list, get branches and tags | `schema.read` |
| create or delete a branch, create a tag | `branch.create` |
| protect or unprotect a branch | `domain.settings` |
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from lorebase_core.canonical import hex
from lorebase_keeper.auth.audit import annotate
from lorebase_keeper.auth.deps import Access, AuthDep, PrincipalDep, authorize, require
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.rbac import AccessContext
from lorebase_keeper.context import StoreDep, parse_revision_id
from lorebase_keeper.deps import IdempotencyKeyDep, KeeperSettingsDep
from lorebase_keeper.errors import Problem, ValidationProblem
from lorebase_keeper.pagination import Page, PageParamsDep, encode_cursor
from lorebase_keeper.routes.domains import REPLAYED_HEADER
from lorebase_keeper.services import branches as service
from lorebase_store.models.enums import Materialization, RefKind
from lorebase_store.repositories import RefRow

router = APIRouter(tags=["branches"])

BRANCH_PATH = "/domains/{domain_id}/branches/{branch:path}"

_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": Problem, "description": "The caller lacks the permission named in the body."},
    404: {"model": Problem, "description": "Domain, branch, tag or Revision not found."},
    409: {"model": Problem, "description": "Exists already, protected, or over a limit."},
    422: {"model": ValidationProblem, "description": "Invalid request."},
    503: {"model": Problem, "description": "The branch is locked by another writer."},
}


class BranchOut(BaseModel):
    """A branch."""

    name: str
    head: str
    protected: bool
    default: bool
    materialization: Materialization
    version: int
    created_at: datetime
    created_by: str


class BranchDetail(BranchOut):
    """A branch with its distance to the default branch."""

    ahead: int = Field(description="Revisions on this branch that the default branch lacks.")
    behind: int = Field(description="Revisions on the default branch that this branch lacks.")


class BranchCreate(BaseModel):
    """A new branch, started at a Revision or at the head of a branch or tag."""

    name: str = Field(min_length=1)
    source: str | None = Field(
        default=None, description="Branch or tag to start from; default: the default branch."
    )
    revision: str | None = Field(
        default=None, description="Revision id to start at; default: the head of `source`."
    )


class BranchUpdate(BaseModel):
    """Branch flags."""

    protected: bool = Field(
        description="Protected branches accept changes only through Proposals (design 6.5)."
    )


class TagOut(BaseModel):
    """An immutable tag."""

    name: str
    revision: str
    created_at: datetime
    created_by: str


class TagCreate(BaseModel):
    """A new tag at a Revision or at the head of a branch."""

    name: str = Field(min_length=1)
    source: str | None = Field(default=None, description="Branch whose head is tagged.")
    revision: str | None = Field(default=None, description="Revision id to tag.")


def _branch(ref: RefRow, *, default: bool) -> BranchOut:
    return BranchOut(
        name=ref.name,
        head=hex(ref.head),
        protected=ref.protected,
        default=default,
        materialization=ref.materialization,
        version=ref.version,
        created_at=ref.created_at,
        created_by=ref.created_by,
    )


def _tag(ref: RefRow) -> TagOut:
    return TagOut(
        name=ref.name, revision=hex(ref.head), created_at=ref.created_at, created_by=ref.created_by
    )


def _after(params: Any) -> str | None:
    return None if params.position is None else str(params.position.get("name", ""))


def _next(rows: list[RefRow], limit: int) -> str | None:
    return encode_cursor({"name": rows[limit - 1].name}) if len(rows) > limit else None


@router.get("/domains/{domain_id}/branches", summary="List branches", responses=_ERRORS)
async def list_branches(
    domain_id: str,
    store: StoreDep,
    params: PageParamsDep,
    _: Annotated[Access, Depends(require(Permission.SCHEMA_READ))],
) -> Page[BranchOut]:
    """Branches ordered by name."""
    default, rows = await service.page(
        store, domain_id, RefKind.BRANCH, limit=params.limit + 1, after=_after(params)
    )
    return Page[BranchOut](
        items=[_branch(r, default=r.name == default) for r in rows[: params.limit]],
        next_cursor=_next(rows, params.limit),
    )


@router.post(
    "/domains/{domain_id}/branches", status_code=201, summary="Create a branch", responses=_ERRORS
)
async def create_branch(
    domain_id: str,
    body: BranchCreate,
    request: Request,
    response: Response,
    principal: PrincipalDep,
    auth: AuthDep,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    idempotency_key: IdempotencyKeyDep,
) -> BranchOut:
    """Create a fully materialized branch from a branch, tag or Revision.

    With an `Idempotency-Key`, retrying a create that succeeded returns the branch with status
    200 and `Idempotent-Replayed: true`.
    """
    await authorize(
        request,
        auth,
        principal,
        Permission.BRANCH_CREATE,
        domain_id,
        AccessContext(branch=body.name),
    )
    revision = None if body.revision is None else parse_revision_id(body.revision, "/body/revision")
    created = await service.create(
        store,
        domain_id,
        body.name,
        principal,
        keeper.branches,
        source=body.source,
        revision=revision,
        idempotency_key=idempotency_key,
    )
    annotate(
        request,
        action="branch.create",
        target=body.name,
        head=hex(created.ref.head),
        source=body.source,
        materialization=None if created.mode is None else created.mode.value,
    )
    if created.replayed:
        response.status_code = 200
        response.headers[REPLAYED_HEADER] = "true"
    response.headers["Location"] = (
        f"{keeper.api_prefix}/domains/{domain_id}/branches/{quote(body.name, safe='')}"
    )
    return _branch(created.ref, default=created.default)


@router.get("/domains/{domain_id}/tags", summary="List tags", responses=_ERRORS)
async def list_tags(
    domain_id: str,
    store: StoreDep,
    params: PageParamsDep,
    _: Annotated[Access, Depends(require(Permission.SCHEMA_READ))],
) -> Page[TagOut]:
    """Tags ordered by name."""
    _default, rows = await service.page(
        store, domain_id, RefKind.TAG, limit=params.limit + 1, after=_after(params)
    )
    return Page[TagOut](
        items=[_tag(r) for r in rows[: params.limit]], next_cursor=_next(rows, params.limit)
    )


@router.post(
    "/domains/{domain_id}/tags", status_code=201, summary="Create a tag", responses=_ERRORS
)
async def create_tag(
    domain_id: str,
    body: TagCreate,
    request: Request,
    response: Response,
    principal: PrincipalDep,
    auth: AuthDep,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    idempotency_key: IdempotencyKeyDep,
) -> TagOut:
    """Tag a Revision. Tags never move."""
    await authorize(
        request,
        auth,
        principal,
        Permission.BRANCH_CREATE,
        domain_id,
        AccessContext(branch=body.name),
    )
    revision = None if body.revision is None else parse_revision_id(body.revision, "/body/revision")
    created = await service.create_tag(
        store,
        domain_id,
        body.name,
        principal,
        keeper.branches,
        source=body.source,
        revision=revision,
        idempotency_key=idempotency_key,
    )
    annotate(request, action="tag.create", target=body.name, revision=hex(created.ref.head))
    if created.replayed:
        response.status_code = 200
        response.headers[REPLAYED_HEADER] = "true"
    return _tag(created.ref)


# Routes with a hierarchical branch name come last: `{branch:path}` matches any suffix.


@router.get(BRANCH_PATH, summary="Get a branch", responses=_ERRORS)
async def get_branch(
    domain_id: str,
    branch: str,
    store: StoreDep,
    _: Annotated[Access, Depends(require(Permission.SCHEMA_READ, branch_param="branch"))],
) -> BranchDetail:
    """The branch head, protection flag and ahead/behind counts against the default branch."""
    state = await service.get(store, domain_id, branch)
    out = _branch(state.ref, default=state.default)
    return BranchDetail(**out.model_dump(), ahead=state.ahead, behind=state.behind)


@router.patch(BRANCH_PATH, summary="Protect or unprotect a branch", responses=_ERRORS)
async def update_branch(
    domain_id: str,
    branch: str,
    body: BranchUpdate,
    request: Request,
    store: StoreDep,
    _: Annotated[Access, Depends(require(Permission.DOMAIN_SETTINGS, branch_param="branch"))],
) -> BranchOut:
    """Set the protection flag."""
    state = await service.set_protected(store, domain_id, branch, body.protected)
    annotate(
        request,
        action="branch.protect" if body.protected else "branch.unprotect",
        target=branch,
    )
    return _branch(state.ref, default=state.default)


@router.delete(BRANCH_PATH, status_code=204, summary="Delete a branch", responses=_ERRORS)
async def delete_branch(
    domain_id: str,
    branch: str,
    request: Request,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    _: Annotated[Access, Depends(require(Permission.BRANCH_CREATE, branch_param="branch"))],
) -> Response:
    """Delete a branch and its materialized rows; the default and protected branches stay."""
    ref = await service.delete(store, domain_id, branch, keeper.branches)
    annotate(request, action="branch.delete", target=branch, head=hex(ref.head))
    return Response(status_code=204)
