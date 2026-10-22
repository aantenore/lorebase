"""Preview of a candidate Derivation (design 5.8, 11.1).

| Route | Permission |
|-------|------------|
| `POST /domains/{d}/derivations:preview` | `data.read` and `schema.read` on the branch |

The body is a schema change set, as for `POST /schema:validate` (`yaml`, or `patch` and
`remove`; an empty `patch` previews the schema as saved), and `derivation`, the api name of the
`expression` or `aggregate` Derivation to evaluate in the schema it produces. The schema is
validated like a schema change (`schema.expression_limits`, the rules of `schema.*`), then the
Derivation is evaluated on `limit` Records of its Kind (or the `keys` named) at the head of the
branch, reading the stored and derived values of the branch. Nothing is written.

Reads follow Record reads: the caller needs `data.read` for every Kind the Derivation touches
(403 otherwise), and a value of an Attribute that is sensitive, or computed from sensitive
Attributes, is masked unless the caller is cleared for it on the Derivation's Kind and on every
Kind the sensitive values come from; a masked item reports a failure without its code or
message. At a Revision other than the branch head the preview is refused. The sample size
defaults to and is capped by `read.derivation_preview`, the length of each of `keys` by
`read.derivation_preview_max_key_length`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel, Field, JsonValue

from lorebase_keeper.auth.audit import annotate
from lorebase_keeper.auth.deps import AuthDep, PrincipalDep
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.deps import KeeperSettingsDep
from lorebase_keeper.errors import Problem, ValidationProblem
from lorebase_keeper.reading.points import resolve_point
from lorebase_keeper.reading.problems import invalid
from lorebase_keeper.reading.runtime import ReadRuntimeDep, ReadSessionDep
from lorebase_keeper.routes.schema import ChangeSetBody
from lorebase_keeper.services import derivation_preview as service
from lorebase_keeper.services.errors import violations_problem
from lorebase_keeper.services.reads import ReadGuard

router = APIRouter(tags=["derivations"])

_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {
        "model": Problem,
        "description": "The caller lacks data.read for a Kind the Derivation touches, or "
        "schema.read.",
    },
    404: {"model": Problem, "description": "Domain, branch or Revision not found."},
    422: {
        "model": ValidationProblem,
        "description": "Invalid request, or a candidate schema that fails validation (`issues` "
        "locate why).",
    },
}


def preview_guard() -> Callable[..., Awaitable[ReadGuard]]:
    """Dependency: the authenticated reader of the Domain named by the path; `data.read` and
    `schema.read` are checked once the read point is resolved."""

    async def dependency(
        request: Request, principal: PrincipalDep, auth: AuthDep, keeper: KeeperSettingsDep
    ) -> ReadGuard:
        domain_id = str(request.path_params["domain_id"])
        annotate(request, domain_id=domain_id, permission=Permission.DATA_READ.value)
        return ReadGuard(
            authorizer=auth.authorizer,
            principal=principal,
            permissions=(Permission.DATA_READ, Permission.SCHEMA_READ),
            domain_id=domain_id,
            max_depth=keeper.reads.reachability_max_depth,
        )

    return dependency


PreviewReader = Annotated[ReadGuard, Depends(preview_guard())]


class DerivationPreviewRequest(ChangeSetBody):
    """A candidate schema change set and the Derivation to evaluate in it."""

    derivation: str = Field(
        min_length=1,
        description="Api name of the `expression` or `aggregate` Derivation to evaluate in the "
        "schema the change set produces.",
    )
    branch: str | None = Field(default=None, description="Default: the default branch.")
    at: str | None = Field(
        default=None, description="Revision id, tag or branch name; only the branch head."
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        description="Records to evaluate; default and maximum: `read.derivation_preview`.",
    )
    keys: list[str] | None = Field(
        default=None,
        min_length=1,
        description="Keys of the Records to evaluate instead of the first ones by key; at most "
        "`read.derivation_preview.max_limit`, each at most "
        "`read.derivation_preview_max_key_length` characters.",
    )


class PreviewErrorOut(BaseModel):
    """Why a value could not be computed for a Record."""

    code: str | None = Field(
        description="Null when the value is masked: only the failure itself is reported."
    )
    message: str | None = Field(description="Null when the value is masked: messages quote values.")


class PreviewItemOut(BaseModel):
    """The computed value for one sample Record."""

    key: str
    title: str | None
    value: JsonValue = Field(
        description="The canonical value; null when there is none, the masked value (or null "
        "with `keeper.reads.sensitivity.omit`) when `masked`."
    )
    masked: bool
    error: PreviewErrorOut | None = None


class DerivationPreviewOut(BaseModel):
    """Values a candidate Derivation computes on sample Records."""

    domain: str
    branch: str
    revision: str = Field(description="Revision whose Records were evaluated.")
    served_from: str
    derivation: str
    kind: str
    attribute: str
    items: list[PreviewItemOut]


def _bounded(count: int | None, default: int, maximum: int, pointer: str) -> int:
    effective = default if count is None else count
    if effective > maximum:
        raise invalid(pointer, f"must not exceed {maximum}", "limit_exceeded")
    return effective


@router.post(
    "/domains/{domain_id}/derivations:preview",
    summary="Preview a candidate Derivation",
    response_model=DerivationPreviewOut,
    responses=_ERRORS,
)
async def preview_derivation(
    domain_id: str,
    payload: Annotated[DerivationPreviewRequest, Body()],
    access: PreviewReader,
    runtime: ReadRuntimeDep,
    session: ReadSessionDep,
    keeper: KeeperSettingsDep,
) -> DerivationPreviewOut:
    """Evaluate a Derivation, as edited and not yet saved, on sample Records of its Kind.

    The candidate schema is checked like a schema change; reading follows Record reads
    (permissions per Kind, masking of sensitive values). Nothing is written.
    """
    bounds = runtime.read.derivation_preview
    limit = _bounded(payload.limit, bounds.default_limit, bounds.max_limit, "/limit")
    if payload.keys is not None:
        _bounded(len(payload.keys), bounds.default_limit, bounds.max_limit, "/keys")
        longest = runtime.read.derivation_preview_max_key_length
        for index, key in enumerate(payload.keys):
            if len(key) > longest:
                raise invalid(
                    f"/keys/{index}", f"must not exceed {longest} characters", "limit_exceeded"
                )
    point = await resolve_point(session, runtime, access, branch=payload.branch, at=payload.at)
    if not point.materialized:
        raise invalid("/at", "A Derivation is previewed on the head of a branch.", "time_travel")
    try:
        candidate = service.candidate_of(
            point.schema, payload.to_input(), payload.derivation, runtime.store.schema.rules()
        )
    except service.CandidateInvalidError as error:
        raise violations_problem(error.issues, limit=keeper.schema_changes.max_issues) from None
    point.scope.require(service.kind_names(candidate.schema, candidate.touched))
    items = await service.preview(
        session, runtime, point, candidate, keys=payload.keys, limit=limit
    )
    return DerivationPreviewOut(
        domain=domain_id,
        branch=point.branch,
        revision=point.revision_hex,
        served_from=point.served_from.value,
        derivation=payload.derivation,
        kind=candidate.kind.api_name,
        attribute=candidate.attribute.api_name,
        items=[
            PreviewItemOut(
                key=item.key,
                title=item.title,
                value=item.value,
                masked=item.masked,
                error=PreviewErrorOut(code=item.error_code, message=item.error_message)
                if item.failed
                else None,
            )
            for item in items
        ],
    )
