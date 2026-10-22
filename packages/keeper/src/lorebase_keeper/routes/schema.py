"""Schema reads and schema change sets (design 11.1).

| Route | Permission |
|-------|------------|
| `GET /domains/{d}/schema`, `GET /domains/{d}/schema/{section}/{name}` | `schema.read` |
| `POST /domains/{d}/schema:validate` | `schema.read` |
| `POST /domains/{d}/branches/{b}/schema/changes` | `schema.write` on the branch |
| `POST /domains/{d}/branches/{b}/rebase-tags` | `schema.write` on the branch |

`schema.read` is checked against the Revision that `branch`/`at` resolve to, not the name given
(`services/reads.py`).

The schema is served as JSON (elements keyed by id, with api names and descriptions) or, with
`Accept: text/yaml` or `format=yaml`, as the YAML document of `lorebase_core.model.yaml_schema`.
A change set is sent as JSON (`yaml`: a full document, or `patch` and `remove`) or as a raw
YAML document (`Content-Type: text/yaml` or `application/yaml`, options as query
parameters). Protected branches refuse direct changes: the change goes to another branch and
then through a Proposal.

`rebase-tags` brings a branch forked before the default branch tagged an Attribute level with
it: one schema change adds the default branch's sensitivity tags (never removes one) and
recomputes the titles and search vectors of the Kinds it changes. With `rows_only`, the schema
is left alone and a `store.sensitivity_refresh` job rewrites those rows in batches.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from pydantic import BaseModel, Field, JsonValue, ValidationError, model_validator

from lorebase_core.canonical import hex
from lorebase_core.model import Schema, Sensitivity, dump_schema
from lorebase_keeper.auth.audit import annotate
from lorebase_keeper.auth.deps import Access, AuthDep, PrincipalDep, require
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.auth.rbac import AccessContext
from lorebase_keeper.auth.runtime import AuthRuntime
from lorebase_keeper.context import StoreAccess, StoreDep, parse_revision_id
from lorebase_keeper.deps import KeeperSettingsDep
from lorebase_keeper.errors import NOT_FOUND, VALIDATION_FAILED, Problem, ProblemError
from lorebase_keeper.reading.runtime import ReadRuntime, ReadRuntimeDep
from lorebase_keeper.reading.sensitivity import clearance_of, default_schema, require_lowering
from lorebase_keeper.services import schema as service
from lorebase_keeper.services.errors import (
    ProtectedBranchError,
    commit_problem,
    finding_documents,
    protected_branch_problem,
    violations_problem,
)
from lorebase_keeper.services.operation_catalog import builtin_definition
from lorebase_keeper.services.reads import ReadTarget, resolve_readable
from lorebase_keeper.settings import KeeperSettings
from lorebase_store.commit import CommitError
from lorebase_store.repositories.scope import scope_to_domain

router = APIRouter(tags=["schema"])

YAML_MEDIA_TYPES = ("text/yaml", "application/yaml", "application/x-yaml")
REVISION_HEADER = "Lorebase-Revision"
Section = Literal["kinds", "relations", "traits", "operations", "derivations", "sources"]

_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": Problem, "description": "The caller lacks the permission named in the body."},
    404: {"model": Problem, "description": "Domain, branch, Revision or element not found."},
    409: {"model": Problem, "description": "Protected branch or moved head."},
    422: {"model": Problem, "description": "The change set is invalid; `issues` locate why."},
    503: {"model": Problem, "description": "The branch is locked by another writer."},
}

_YAML_BODY = {
    "requestBody": {
        "content": {
            "text/yaml": {"schema": {"type": "string", "description": "Full schema document."}}
        }
    }
}


_BODY_DOC = (
    "JSON change set, or the full schema document as text/yaml (options as query parameters)."
)


class ElementRemoval(BaseModel):
    """An element to remove."""

    section: Section
    name: str = Field(min_length=1, description="Api name or id of the element.")


class ChangeSetBody(BaseModel):
    """A full schema document, or a patch of elements."""

    yaml: str | None = Field(
        default=None,
        description="Full schema document; elements it leaves out are removed.",
    )
    patch: dict[str, JsonValue] | None = Field(
        default=None,
        description="Partial document (same layout as the YAML document): each element "
        "replaces the element with the same api name, or is added.",
    )
    remove: list[ElementRemoval] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_form(self) -> Self:
        if (self.yaml is not None) == (self.patch is not None or bool(self.remove)):
            raise ValueError("give either `yaml`, or `patch` and/or `remove`")
        return self

    def to_input(self) -> service.ChangeSetInput:
        return service.ChangeSetInput(
            yaml=self.yaml,
            patch=self.patch,
            remove=[service.ElementRemoval(r.section, r.name) for r in self.remove],
        )


class SchemaChangeRequest(ChangeSetBody):
    """A schema change set for one branch."""

    message: str | None = Field(default=None, max_length=2000)
    expected_head: str | None = Field(
        default=None, description="Fail with 409 unless the branch head is this Revision."
    )
    dry_run: bool = Field(default=False, description="Validate and plan without committing.")
    cascade: bool = Field(
        default=False,
        description="Confirm that removing Kinds or Relations deletes their Records or Links.",
    )


class SchemaValidateRequest(ChangeSetBody):
    """A change set to validate against the schema of a branch or Revision."""

    branch: str | None = None
    at: str | None = Field(default=None, description="Revision id, tag or branch name.")


class ElementChangeOut(BaseModel):
    """One element written or removed."""

    op: Literal["create", "update", "delete"]
    section: str
    id: str
    api_name: str


class EmbeddingOut(BaseModel):
    """The embedding model of a Domain: what a caller embeds `query_vector` with."""

    model: str = Field(description="Name the stored vectors are keyed by.")
    dims: int = Field(description="Length of a query vector.")


class SchemaSearchOut(BaseModel):
    """How the Domain can be searched at this Revision."""

    modes: list[Literal["lexical", "semantic", "hybrid"]]
    embedding: EmbeddingOut | None = Field(
        description="Set when some Kind has `search.embedding_template`: semantic and hybrid "
        "search need a `query_vector` made by this model; null otherwise."
    )


class SchemaOut(BaseModel):
    """The schema of a Revision."""

    domain: str
    branch: str | None
    revision: str
    schema_: dict[str, Any] = Field(alias="schema")
    search: SchemaSearchOut


class SchemaChangeOut(BaseModel):
    """Outcome of a schema change set."""

    status: Literal["committed", "dry_run", "no_op"]
    branch: str
    head: str = Field(description="Branch head the change set was applied to.")
    revision: str | None = Field(description="New Revision; null for dry runs.")
    changes: list[ElementChangeOut]
    change_count: int
    data_changes: int = Field(description="Record and Link changes made by data migrations.")
    issues: list[dict[str, Any]]
    issue_count: int


class RebaseTagsRequest(BaseModel):
    """A tag rebase of one branch."""

    message: str | None = Field(default=None, max_length=2000)
    expected_head: str | None = Field(
        default=None, description="Fail with 409 unless the branch head is this Revision."
    )
    dry_run: bool = Field(default=False, description="Plan without committing or queueing.")
    rows_only: bool = Field(
        default=False,
        description="Leave the branch's schema as it is: queue the batched rewrite of the "
        "titles and search vectors it materialized before the default branch's tags.",
    )


class TagChangeOut(BaseModel):
    """Sensitivity tags added to one Attribute."""

    section: Literal["kinds", "relations"]
    owner: str = Field(description="Api name of the Kind or Relation.")
    attribute: str = Field(description="Api name of the Attribute.")
    tags: list[Sensitivity]


class RebaseTagsOut(BaseModel):
    """Outcome of a tag rebase."""

    status: Literal["committed", "dry_run", "no_op", "queued"]
    branch: str
    head: str = Field(description="Branch head the rebase was planned against.")
    revision: str | None = Field(description="New Revision; null for dry runs and rows_only.")
    tags: list[TagChangeOut] = Field(description="Tags the schema change adds.")
    changes: list[ElementChangeOut]
    change_count: int
    issues: list[dict[str, Any]]
    issue_count: int
    job: int | None = Field(
        default=None, description="The queued store.sensitivity_refresh job (rows_only)."
    )


class SchemaValidationOut(BaseModel):
    """Static validation of a change set."""

    valid: bool
    issues: list[dict[str, Any]]
    issue_count: int
    changes: list[ElementChangeOut]


def _invalid_body(error: ValidationError) -> ProblemError:
    errors = [
        {
            "pointer": "/body/" + "/".join(str(p) for p in e["loc"]),
            "message": str(e["msg"]),
            "code": str(e["type"]),
        }
        for e in error.errors()
    ]
    return ProblemError(
        VALIDATION_FAILED,
        "One or more request members are invalid.",
        extensions={"errors": errors},
    )


def _parse[M: BaseModel](body: M | str, model: type[M], options: dict[str, Any]) -> M:
    """The request model; a YAML document body takes its options from the query."""
    if not isinstance(body, str):
        return body
    try:
        return model.model_validate(
            {"yaml": body, **{k: v for k, v in options.items() if v is not None}}
        )
    except ValidationError as error:
        raise _invalid_body(error) from None


def _wants_yaml(request: Request, format_: str | None) -> bool:
    if format_ is not None:
        return format_ == "yaml"
    accept = request.headers.get("accept", "")
    return any(media in accept for media in YAML_MEDIA_TYPES)


async def _readable(
    request: Request,
    auth: AuthRuntime,
    principal: Principal,
    store: StoreAccess,
    keeper: KeeperSettings,
    domain_id: str,
    branch: str | None,
    at: str | None,
) -> ReadTarget:
    """The Revision a read names, once the caller may read it (not just the named branch)."""
    annotate(request, domain_id=domain_id, permission=Permission.SCHEMA_READ.value)
    return await resolve_readable(
        store,
        auth.authorizer,
        principal,
        (Permission.SCHEMA_READ,),
        domain_id,
        branch=branch,
        at=at,
        max_depth=keeper.reads.reachability_max_depth,
    )


def _changes(changes: list[service.ElementChange], limit: int) -> list[ElementChangeOut]:
    return [
        ElementChangeOut(op=c.op, section=c.section, id=c.id, api_name=c.api_name)
        for c in changes[:limit]
    ]


@router.get(
    "/domains/{domain_id}/schema",
    summary="Get the schema",
    responses={**_ERRORS, 200: {"content": {"text/yaml": {}}}},
    response_model=SchemaOut,
)
async def get_schema(
    domain_id: str,
    request: Request,
    principal: PrincipalDep,
    auth: AuthDep,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    runtime: ReadRuntimeDep,
    branch: Annotated[str | None, Query(description="Default: the default branch.")] = None,
    at: Annotated[str | None, Query(description="Revision id, tag or branch name.")] = None,
    format_: Annotated[Literal["json", "yaml"] | None, Query(alias="format")] = None,
) -> Response:
    """The schema at a branch head or at a Revision, as JSON or YAML."""
    target = await _readable(request, auth, principal, store, keeper, domain_id, branch, at)
    schema = await service.read(store, domain_id, target)
    revision = hex(target.revision)
    headers = {REVISION_HEADER: revision}
    if _wants_yaml(request, format_):
        return Response(dump_schema(schema), media_type="text/yaml", headers=headers)
    body = SchemaOut(
        domain=domain_id,
        branch=target.ref,
        revision=revision,
        schema=schema.content(),
        search=_search(schema, runtime),
    )
    return Response(
        body.model_dump_json(by_alias=True), media_type="application/json", headers=headers
    )


def _search(schema: Schema, runtime: ReadRuntime) -> SchemaSearchOut:
    """Search modes of the schema; the embedding model comes from the settings (vectors are
    keyed by its name), never from the inference package."""
    if not any(kind.search.embedding_template for kind in schema.kinds.values()):
        return SchemaSearchOut(modes=["lexical"], embedding=None)
    embedding = EmbeddingOut(
        model=runtime.search.semantic.model, dims=runtime.store_settings.embeddings.dims
    )
    return SchemaSearchOut(modes=["lexical", "semantic", "hybrid"], embedding=embedding)


def _element(schema: Schema, section: Section, name: str) -> dict[str, Any] | None:
    elements: dict[str, Any] = getattr(schema, section)
    found = elements.get(name) or next((e for e in elements.values() if e.api_name == name), None)
    if found is not None:
        return dict(found.content())
    if section == "operations":
        builtin = builtin_definition(schema, name)
        return None if builtin is None else builtin.document()
    return None


@router.get(
    "/domains/{domain_id}/schema/{section}/{name}",
    summary="Get a schema element",
    responses=_ERRORS,
)
async def get_element(
    domain_id: str,
    section: Section,
    name: str,
    request: Request,
    principal: PrincipalDep,
    auth: AuthDep,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    response: Response,
    branch: Annotated[str | None, Query(description="Default: the default branch.")] = None,
    at: Annotated[str | None, Query(description="Revision id, tag or branch name.")] = None,
) -> dict[str, Any]:
    """A Kind, Relation, Trait, Operation, Derivation or Source by api name or id.

    Built-in Operations (`create_supplier`, `link_supplied_by`, `import_records`, ...) are
    described with the parameters derived from the schema.
    """
    target = await _readable(request, auth, principal, store, keeper, domain_id, branch, at)
    schema = await service.read(store, domain_id, target)
    element = _element(schema, section, name)
    if element is None:
        raise ProblemError(NOT_FOUND, f"No element {name!r} in {section}.")
    response.headers[REVISION_HEADER] = hex(target.revision)
    return element


@router.post(
    "/domains/{domain_id}/schema:validate",
    summary="Validate a schema change set",
    responses=_ERRORS,
    openapi_extra=_YAML_BODY,
)
async def validate_schema(
    domain_id: str,
    payload: Annotated[SchemaValidateRequest | str, Body(description=_BODY_DOC)],
    request: Request,
    principal: PrincipalDep,
    auth: AuthDep,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    branch: Annotated[str | None, Query(description="For YAML bodies.")] = None,
    at: Annotated[str | None, Query(description="For YAML bodies.")] = None,
) -> SchemaValidationOut:
    """Static validation of a document or patch against the schema of a branch or Revision.

    Issue paths point into the schema document (api names). Nothing is written.
    """
    body = _parse(payload, SchemaValidateRequest, {"branch": branch, "at": at})
    target = await _readable(
        request, auth, principal, store, keeper, domain_id, body.branch, body.at
    )
    limits = keeper.schema_changes
    issues, changes = await service.validate(store, domain_id, body.to_input(), target)
    return SchemaValidationOut(
        valid=not issues,
        issues=finding_documents(issues, limits.max_issues),
        issue_count=len(issues),
        changes=_changes(changes, limits.max_listed_changes),
    )


@router.post(
    "/domains/{domain_id}/branches/{branch:path}/schema/changes",
    summary="Apply a schema change set",
    responses=_ERRORS,
    openapi_extra=_YAML_BODY,
)
async def apply_schema_changes(
    domain_id: str,
    branch: str,
    payload: Annotated[SchemaChangeRequest | str, Body(description=_BODY_DOC)],
    request: Request,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    access: Annotated[Access, Depends(require(Permission.SCHEMA_WRITE, branch_param="branch"))],
    auth: AuthDep,
    dry_run: Annotated[bool | None, Query(description="For YAML bodies.")] = None,
    cascade: Annotated[bool | None, Query(description="For YAML bodies.")] = None,
    message: Annotated[str | None, Query(description="For YAML bodies.")] = None,
    expected_head: Annotated[str | None, Query(description="For YAML bodies.")] = None,
) -> SchemaChangeOut:
    """Apply a change set as one Revision (data migrations included), or dry-run it.

    A dry run returns the element changes, the number of data changes and the validation
    issues without committing. Protected branches refuse direct changes (409).
    """
    body = _parse(
        payload,
        SchemaChangeRequest,
        {
            "dry_run": dry_run,
            "cascade": cascade,
            "message": message,
            "expected_head": expected_head,
        },
    )
    limits = keeper.schema_changes
    expected = (
        None
        if body.expected_head is None
        else parse_revision_id(body.expected_head, "/body/expected_head")
    )
    async with store.sessions() as session, session.begin():
        await scope_to_domain(session, domain_id)
        reference = await default_schema(session, domain_id, store.runtime)
    # Kind-narrowed grants count for what issues show; removing tags needs Domain-wide ones.
    clearance = await clearance_of(auth.authorizer, access.principal, domain_id, reference)
    try:
        outcome = await service.apply(
            store,
            domain_id,
            branch,
            body.to_input(),
            access.principal,
            message=body.message,
            expected_head=expected,
            dry_run=body.dry_run,
            cascade=body.cascade,
            guard=lambda old, new: require_lowering(
                auth.authorizer, access.principal, domain_id, old, new
            ),
            cleared=clearance.everywhere,
            clearance=clearance,
        )
    except (service.ChangeSetInvalidError, service.RemovalBlockedError) as invalid:
        raise violations_problem(invalid.issues, limit=limits.max_issues) from None
    except ProtectedBranchError:
        raise protected_branch_problem(
            domain_id, branch, f"{keeper.api_prefix}/domains/{domain_id}/proposals"
        ) from None
    except CommitError as error:
        raise commit_problem(
            error,
            max_issues=limits.max_issues,
            retry_after_s=keeper.branches.lock_retry_after_s,
        ) from None
    revision = None if outcome.revision is None else hex(outcome.revision)
    annotate(
        request,
        action="schema.dry_run" if body.dry_run else "schema.change",
        target=branch,
        status=outcome.status.value,
        revision=revision,
        changes=len(outcome.changes),
    )
    return SchemaChangeOut(
        status=outcome.status.value,
        branch=branch,
        head=hex(outcome.head),
        revision=revision,
        changes=_changes(outcome.changes, limits.max_listed_changes),
        change_count=len(outcome.changes),
        data_changes=outcome.data_changes,
        issues=finding_documents(outcome.issues, limits.max_issues),
        issue_count=len(outcome.issues),
    )


@router.post(
    "/domains/{domain_id}/branches/{branch:path}/rebase-tags",
    summary="Take the default branch's sensitivity tags",
    responses=_ERRORS,
)
async def rebase_tags(
    domain_id: str,
    branch: str,
    body: RebaseTagsRequest,
    request: Request,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    access: Annotated[Access, Depends(require(Permission.SCHEMA_WRITE, branch_param="branch"))],
    auth: AuthDep,
) -> RebaseTagsOut:
    """Give a branch forked before the default branch tagged an Attribute the tags it lacks.

    One schema change adds, to every Attribute the branch shares with the default branch, the
    tags the default branch gives it, and to derived Attributes the tags of what they read;
    no tag is removed. It recomputes the titles and search vectors of the Kinds it changes,
    so verification no longer reports them as `sensitivity_stale`. A branch that already has
    the tags gives `no_op`. Protected branches refuse the change (409) unless `rows_only`:
    then only a `store.sensitivity_refresh` job is queued, which rewrites those rows in
    batches and leaves the schema as it is. `tags` lists the tags added only to callers
    who may read the default branch's schema (empty otherwise).
    """
    limits = keeper.schema_changes
    if body.rows_only:
        head, job = await service.queue_tag_refresh(store, domain_id, branch, dry_run=body.dry_run)
        annotate(request, action="schema.rebase_tags", target=branch, job=job)
        return RebaseTagsOut(
            status="dry_run" if body.dry_run else "queued",
            branch=branch,
            head=hex(head),
            revision=None,
            tags=[],
            changes=[],
            change_count=0,
            issues=[],
            issue_count=0,
            job=job,
        )
    expected = (
        None
        if body.expected_head is None
        else parse_revision_id(body.expected_head, "/body/expected_head")
    )
    try:
        rebase = await service.rebase_tags(
            store,
            domain_id,
            branch,
            access.principal,
            message=body.message,
            expected_head=expected,
            dry_run=body.dry_run,
        )
    except service.ChangeSetInvalidError as invalid:
        raise violations_problem(invalid.issues, limit=limits.max_issues) from None
    except ProtectedBranchError:
        raise protected_branch_problem(
            domain_id, branch, f"{keeper.api_prefix}/domains/{domain_id}/proposals"
        ) from None
    except CommitError as error:
        raise commit_problem(
            error,
            max_issues=limits.max_issues,
            retry_after_s=keeper.branches.lock_retry_after_s,
        ) from None
    outcome = rebase.outcome
    revision = None if outcome.revision is None else hex(outcome.revision)
    shown = rebase.tags
    if shown and not await auth.authorizer.allows(
        access.principal,
        Permission.SCHEMA_READ,
        domain_id,
        AccessContext(branch=rebase.default_branch),
    ):
        shown = []  # the names of the tagged Attributes come from the default branch
    annotate(
        request,
        action="schema.rebase_tags",
        target=branch,
        status=outcome.status.value,
        revision=revision,
        changes=len(outcome.changes),
    )
    return RebaseTagsOut(
        status=outcome.status.value,
        branch=branch,
        head=hex(outcome.head),
        revision=revision,
        tags=[
            TagChangeOut(
                section="kinds" if t.section == "kinds" else "relations",
                owner=t.owner,
                attribute=t.attribute,
                tags=t.tags,
            )
            for t in shown
        ],
        changes=_changes(outcome.changes, limits.max_listed_changes),
        change_count=len(outcome.changes),
        issues=finding_documents(outcome.issues, limits.max_issues),
        issue_count=len(outcome.issues),
    )
