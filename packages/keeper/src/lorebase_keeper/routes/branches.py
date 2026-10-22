"""Branches and tags (design 6.5, 11.1).

Branch names are hierarchical (`agent/bot-1/task`); in paths they are written as they are or
URL-encoded. Grants narrowed to a branch pattern apply to the branch named in the path or,
on creation, in the body.

| Route | Permission |
|-------|------------|
| list, get branches and tags | `schema.read` |
| create or delete a branch, create a tag | `branch.create`, plus read of the start point |
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
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.auth.rbac import AccessContext
from lorebase_keeper.auth.runtime import AuthRuntime
from lorebase_keeper.context import StoreAccess, StoreDep, parse_revision_id
from lorebase_keeper.deps import IdempotencyKeyDep, KeeperSettingsDep
from lorebase_keeper.errors import Problem, ValidationProblem
from lorebase_keeper.pagination import Page, PageParamsDep, encode_cursor
from lorebase_keeper.routes.domains import REPLAYED_HEADER
from lorebase_keeper.services import branches as service
from lorebase_keeper.services.reads import resolve_readable
from lorebase_keeper.settings import KeeperSettings
from lorebase_store.materialize import BranchMode
from lorebase_store.models.enums import Materialization, RefKind
from lorebase_store.repositories import (
    MaterializationPhase,
    MaterializationState,
    MaterializationStatus,
    RefRow,
)

router = APIRouter(tags=["branches"])

BRANCH_PATH = "/domains/{domain_id}/branches/{branch:path}"

_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": Problem, "description": "The caller lacks the permission named in the body."},
    404: {"model": Problem, "description": "Domain, branch, tag or Revision not found."},
    409: {"model": Problem, "description": "Exists already, protected, or over a limit."},
    422: {"model": ValidationProblem, "description": "Invalid request."},
    503: {"model": Problem, "description": "The branch is locked by another writer."},
}


class MaterializationProgress(BaseModel):
    """How far the background copy of a branch got."""

    phase: MaterializationPhase = Field(
        description="What the next batch copies: `records`, `links`, `history`, then `switch` "
        "(the branch becomes a full copy); `done` at the end."
    )
    rows_done: int = Field(description="Rows of the source branch read so far.")
    rows_total: int | None = Field(
        description="Rows of the source branch to read (Records, Links and history entries "
        "at the start of the copy); null until the job has started."
    )
    error: str | None = Field(
        description="Last failure of the copy job, kept while it is retried and when it failed."
    )
    updated_at: datetime
    finished_at: datetime | None


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
    materialization_status: MaterializationStatus | None = Field(
        default=None,
        description="Background copy of the branch: a large copy branch is created as an "
        "overlay of its source (`materialization` is `overlay`, reads and writes work at once) "
        "and a job copies its rows in batches; a large copy that cannot start as an overlay "
        "(of an overlay, from an older Revision) is `building` until a job writes its rows "
        "from its tree (its head reads like a past Revision, commits answer 409). `pending` "
        "(queued, or waiting for a retry), `running`, `done` (`materialization` is then "
        "`full`) or `failed` (a build that failed for good). Null for a branch that was never "
        "copied in the background.",
    )
    materialization_progress: MaterializationProgress | None = Field(
        default=None, description="Progress of the background copy; null without one."
    )


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
    mode: BranchMode | None = Field(
        default=None,
        description="`copy`: the branch gets its own materialized rows. `overlay`: it stores "
        "only the rows it changes and reads the rest from `source` as of its head (only when "
        "starting at the head of a fully materialized branch; otherwise a copy). Default: "
        "`store.materialize.branch_mode`, overlay for names with an overlay prefix.",
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


def _branch(ref: RefRow, *, default: bool, state: MaterializationState | None = None) -> BranchOut:
    progress = None
    if state is not None:
        progress = MaterializationProgress(
            phase=state.phase,
            rows_done=state.rows_done,
            rows_total=state.rows_total,
            error=state.error,
            updated_at=state.updated_at,
            finished_at=state.finished_at,
        )
    return BranchOut(
        name=ref.name,
        head=hex(ref.head),
        protected=ref.protected,
        default=default,
        materialization=ref.materialization,
        version=ref.version,
        created_at=ref.created_at,
        created_by=ref.created_by,
        materialization_status=None if state is None else state.status,
        materialization_progress=progress,
    )


def _tag(ref: RefRow) -> TagOut:
    return TagOut(
        name=ref.name, revision=hex(ref.head), created_at=ref.created_at, created_by=ref.created_by
    )


async def _require_source_readable(
    auth: AuthRuntime,
    principal: Principal,
    store: StoreAccess,
    keeper: KeeperSettings,
    domain_id: str,
    *,
    source: str | None,
    revision: str | None,
) -> None:
    """A new branch or tag starts at content the caller must be able to read: the Revision
    given, else the source branch or tag (default: the default branch)."""
    await resolve_readable(
        store,
        auth.authorizer,
        principal,
        (Permission.SCHEMA_READ, Permission.DATA_READ),
        domain_id,
        branch=None,
        at=revision or source,
        max_depth=keeper.reads.reachability_max_depth,
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
    found = await service.page(
        store, domain_id, RefKind.BRANCH, limit=params.limit + 1, after=_after(params)
    )
    return Page[BranchOut](
        items=[
            _branch(r, default=r.name == found.default, state=found.materializations.get(r.name))
            for r in found.refs[: params.limit]
        ],
        next_cursor=_next(found.refs, params.limit),
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
    """Create a materialized branch (a copy, or an overlay on its source) from a branch, tag
    or Revision.

    A copy of a large source (above `store.materialize.async_copy_min_rows` rows) is created
    as an overlay at once and copied by a background job: `materialization_status` is then
    `pending`, and the branch becomes `full` when it is `done`. With an `Idempotency-Key`,
    retrying a create that succeeded returns the branch with status 200 and
    `Idempotent-Replayed: true`.
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
    await _require_source_readable(
        auth, principal, store, keeper, domain_id, source=body.source, revision=body.revision
    )
    created = await service.create(
        store,
        domain_id,
        body.name,
        principal,
        keeper.branches,
        source=body.source,
        revision=revision,
        idempotency_key=idempotency_key,
        mode=body.mode,
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
    return _branch(created.ref, default=created.default, state=created.materialization)


@router.get("/domains/{domain_id}/tags", summary="List tags", responses=_ERRORS)
async def list_tags(
    domain_id: str,
    store: StoreDep,
    params: PageParamsDep,
    _: Annotated[Access, Depends(require(Permission.SCHEMA_READ))],
) -> Page[TagOut]:
    """Tags ordered by name."""
    found = await service.page(
        store, domain_id, RefKind.TAG, limit=params.limit + 1, after=_after(params)
    )
    return Page[TagOut](
        items=[_tag(r) for r in found.refs[: params.limit]],
        next_cursor=_next(found.refs, params.limit),
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
    await _require_source_readable(
        auth, principal, store, keeper, domain_id, source=body.source, revision=body.revision
    )
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
    out = _branch(state.ref, default=state.default, state=state.materialization)
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
    """Delete a branch and its materialized rows; the default and protected branches stay.

    A large branch (above `store.materialize.async_delete_min_rows` rows) disappears at once
    and a background job deletes its rows; its name can be used again when that is done.
    """
    ref, deletion = await service.delete(store, domain_id, branch, keeper.branches)
    annotate(
        request, action="branch.delete", target=branch, head=hex(ref.head), deletion=deletion.value
    )
    return Response(status_code=204)
