"""Domains (design 5.2, 11.1): create, list, read, update display metadata, delete.

Creating a Domain needs the installation-level `domain.create` permission; the creator gets
the configured role (`keeper.domains.creator_role`, default `admin`) in the new Domain.
Deleting one needs `domain.delete`, which only the Domain's `admin` role holds, and the Domain
id repeated in `confirm`: the deletion removes every Revision, branch and grant of the Domain
and cannot be undone (the audit log keeps its entries).

`PATCH` also replaces the allowlisted settings members (`extraction`, the ingestion mode of
design 9.2; `null` removes it), with `domain.settings`, audited as `domain.update` with the
member's values before and after. `GET` reports `ignored_settings`: the paths of extraction
values that do not apply because they loosen a member the installation does not let Domains
loosen (`scribe.extraction.domain_may_loosen`, read from this process's configuration) or are
invalid; Scribe resolves the same settings with its own configuration when it runs a job.
`GET` and `PATCH` also report `can`: what the caller may do with the Domain itself (for
clients that show or hide controls), computed by the authorizer like the 403s it predicts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field, JsonValue

from lorebase_core.canonical import hex
from lorebase_core.model import Domain
from lorebase_keeper.auth.audit import annotate
from lorebase_keeper.auth.deps import (
    Access,
    AuthDep,
    PrincipalDep,
    limited_kinds,
    require,
    require_installation,
)
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.auth.rbac import NO_CONTEXT
from lorebase_keeper.auth.runtime import AuthRuntime
from lorebase_keeper.context import StoreDep
from lorebase_keeper.deps import IdempotencyKeyDep, KeeperSettingsDep, SettingsDep
from lorebase_keeper.errors import VALIDATION_FAILED, Problem, ProblemError, ValidationProblem
from lorebase_keeper.pagination import Page, PageParamsDep, encode_cursor
from lorebase_keeper.services import domains as service
from lorebase_keeper.services.branches import check_ref_name
from lorebase_platform.settings import Settings
from lorebase_store.extraction import modes
from lorebase_store.repositories import DomainRow

router = APIRouter(tags=["domains"])

DOMAIN_ID_PATTERN = r"^[a-z][a-z0-9_-]*$"
REPLAYED_HEADER = "Idempotent-Replayed"

_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": Problem, "description": "The caller lacks the permission named in the body."},
    404: {"model": Problem, "description": "Domain not found."},
    409: {"model": Problem, "description": "The Domain already exists."},
    422: {"model": ValidationProblem, "description": "Invalid request."},
}


class DomainCreate(BaseModel):
    """A new Domain."""

    id: str = Field(pattern=DOMAIN_ID_PATTERN, max_length=63, description="Immutable slug.")
    display_name: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=4000)
    default_branch: str = Field(default="main", min_length=1, max_length=200)
    settings: dict[str, JsonValue] = Field(
        default_factory=dict, description="Search language, materialization and merge policies."
    )


UPDATABLE_SETTINGS = (modes.SETTING_KEY,)
"""Domain settings members `PATCH /domains/{d}` may replace."""


class DomainUpdate(BaseModel):
    """Display metadata and allowlisted settings members; omitted members are left
    unchanged."""

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    settings: dict[str, JsonValue] | None = Field(
        default=None,
        description="Settings members to replace, `null` to remove one: `extraction` (`mode`: "
        "review_all, doubts_only or direct; `auto_accept_threshold`; `min_confidence`; "
        "`rules`: [{source_key_prefix, mode, auto_accept_threshold, min_confidence}]).",
    )


class DomainCan(BaseModel):
    """What the caller may do with the Domain itself."""

    settings: bool = Field(
        description="The caller holds `domain.settings`: it may `PATCH` the Domain."
    )


class DomainOut(BaseModel):
    """A Domain and the head Revision of its default branch."""

    id: str
    display_name: str
    description: str | None
    default_branch: str
    settings: dict[str, Any]
    created_at: datetime
    created_by: str
    head: str | None = Field(description="Head Revision of the default branch.")
    ignored_settings: list[str] = Field(
        default_factory=list,
        description="Paths of settings values that do not apply (invalid, or loosening a "
        "member the installation does not let Domains loosen), e.g. `extraction.mode`.",
    )
    can: DomainCan | None = Field(
        default=None,
        description="What the caller may do with the Domain; reported by `GET` and `PATCH` of "
        "one Domain, null in lists.",
    )


def installation_bounds(settings: Settings) -> modes.Installation:
    """The extraction bounds of the installation (`scribe.extraction`), as configured for this
    process."""
    scribe = (settings.model_extra or {}).get("scribe")
    raw = scribe.get(modes.SETTING_KEY) if isinstance(scribe, dict) else None
    return modes.Installation.from_section(raw if isinstance(raw, dict) else None)


def _settings_problems(settings: dict[str, Any] | None, pointer: str) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for key, value in (settings or {}).items():
        if key == modes.SETTING_KEY:
            found.extend(
                {"pointer": f"{pointer}/{key}{at}", "message": message, "code": "invalid"}
                for at, message in modes.setting_problems(value)
            )
    return found


def _require_valid(problems: list[dict[str, str]]) -> None:
    if problems:
        raise ProblemError(
            VALIDATION_FAILED,
            "One or more request members are invalid.",
            extensions={"errors": problems},
        )


async def _can(auth: AuthRuntime, principal: Principal, domain_id: str) -> DomainCan:
    return DomainCan(
        settings=await auth.authorizer.allows(
            principal, Permission.DOMAIN_SETTINGS, domain_id, NO_CONTEXT
        )
    )


def _out(
    row: DomainRow,
    head: bytes | None,
    bounds: modes.Installation | None = None,
    can: DomainCan | None = None,
) -> DomainOut:
    return DomainOut(
        can=can,
        ignored_settings=[] if bounds is None else modes.ignored_settings(bounds, row.settings),
        id=row.id,
        display_name=row.display_name,
        description=row.description,
        default_branch=row.default_branch,
        settings=dict(row.settings),
        created_at=row.created_at,
        created_by=row.created_by,
        head=None if head is None else hex(head),
    )


@router.post("/domains", status_code=201, summary="Create a Domain", responses=_ERRORS)
async def create_domain(
    body: DomainCreate,
    request: Request,
    response: Response,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    auth: AuthDep,
    idempotency_key: IdempotencyKeyDep,
    access: Annotated[Access, Depends(require_installation(Permission.DOMAIN_CREATE))],
) -> DomainOut:
    """Create a Domain with an initial Revision on its default branch (protected by default).

    With an `Idempotency-Key`, retrying a create that succeeded returns the Domain with
    status 200 and `Idempotent-Replayed: true`.
    """
    check_ref_name(body.default_branch, keeper.branches, "/body/default_branch")
    _require_valid(_settings_problems(body.settings, "/body/settings"))
    domain = Domain(
        id=body.id,
        display_name=body.display_name,
        description=body.description,
        default_branch=body.default_branch,
        settings=body.settings,
    )
    created = await service.create(
        store, domain, access.principal, keeper.domains, idempotency_key=idempotency_key
    )
    auth.authorizer.invalidate()
    annotate(request, domain_id=body.id, action="domain.create", target=body.id)
    if created.replayed:
        response.status_code = 200
        response.headers[REPLAYED_HEADER] = "true"
    response.headers["Location"] = f"{keeper.api_prefix}/domains/{body.id}"
    return _out(created.domain, created.head)


@router.get("/domains", summary="List Domains", responses=_ERRORS)
async def list_domains(
    principal: PrincipalDep, auth: AuthDep, store: StoreDep, params: PageParamsDep
) -> Page[DomainOut]:
    """Domains whose schema the caller may read (also through grants narrowed to Kinds that
    are invisible to it otherwise), ordered by id."""
    after = None if params.position is None else str(params.position.get("id", ""))
    items: list[DomainOut] = []
    more = True
    # Domains the caller cannot see are skipped: read further batches until the page is full.
    while more and len(items) < params.limit:
        rows = await service.page(store, limit=params.limit, after=after)
        more = len(rows) == params.limit
        for position, row in enumerate(rows):
            after = row.id
            if await auth.authorizer.allows(
                principal, Permission.SCHEMA_READ, row.id
            ) or await limited_kinds(auth.authorizer, principal, Permission.SCHEMA_READ, row.id):
                items.append(_out(row, None))
            if len(items) == params.limit:
                more = more or position < len(rows) - 1
                break
    next_cursor = encode_cursor({"id": after}) if more and after is not None else None
    return Page[DomainOut](items=items, next_cursor=next_cursor)


@router.get("/domains/{domain_id}", summary="Get a Domain", responses=_ERRORS)
async def get_domain(
    domain_id: str,
    store: StoreDep,
    settings: SettingsDep,
    auth: AuthDep,
    access: Annotated[Access, Depends(require(Permission.SCHEMA_READ, kind_limited=True))],
) -> DomainOut:
    """The Domain, the head Revision of its default branch, its ignored settings and what the
    caller may do with it."""
    row, head = await service.get(store, domain_id)
    can = await _can(auth, access.principal, domain_id)
    return _out(row, head, installation_bounds(settings), can)


@router.patch(
    "/domains/{domain_id}", summary="Update Domain metadata and settings", responses=_ERRORS
)
async def update_domain(
    domain_id: str,
    body: DomainUpdate,
    request: Request,
    store: StoreDep,
    settings: SettingsDep,
    auth: AuthDep,
    access: Annotated[Access, Depends(require(Permission.DOMAIN_SETTINGS))],
) -> DomainOut:
    """Change the display name, the description or allowlisted settings members (`extraction`).
    They are not versioned; setting changes are audited with their values before and after."""
    unknown = sorted(set(body.settings or {}) - set(UPDATABLE_SETTINGS))
    _require_valid(
        [
            {
                "pointer": f"/body/settings/{key}",
                "message": "not a setting this request may change "
                f"({', '.join(UPDATABLE_SETTINGS)})",
                "code": "invalid",
            }
            for key in unknown
        ]
        + _settings_problems(body.settings, "/body/settings")
    )
    _row, changes = await service.update(
        store,
        domain_id,
        display_name=body.display_name,
        description=body.description,
        settings=body.settings,
    )
    annotate(
        request,
        action="domain.update",
        target=domain_id,
        fields=sorted(body.model_dump(exclude_unset=True)),
        settings=[{"member": c.member, "before": c.before, "after": c.after} for c in changes],
    )
    row, head = await service.get(store, domain_id)
    return _out(
        row, head, installation_bounds(settings), await _can(auth, access.principal, domain_id)
    )


@router.delete(
    "/domains/{domain_id}", status_code=204, summary="Delete a Domain", responses=_ERRORS
)
async def delete_domain(
    domain_id: str,
    request: Request,
    store: StoreDep,
    auth: AuthDep,
    confirm: Annotated[str, Query(description="The Domain id, repeated to confirm.")],
    _: Annotated[Access, Depends(require(Permission.DOMAIN_DELETE))],
) -> Response:
    """Delete the Domain with all its Revisions, branches, materialized rows and grants."""
    if confirm != domain_id:
        raise ProblemError(
            VALIDATION_FAILED,
            "One or more request members are invalid.",
            extensions={
                "errors": [
                    {
                        "pointer": "/query/confirm",
                        "message": "must repeat the Domain id",
                        "code": "invalid",
                    }
                ]
            },
        )
    await service.delete(store, domain_id)
    auth.authorizer.invalidate()
    annotate(request, action="domain.delete", target=domain_id)
    return Response(status_code=204)
