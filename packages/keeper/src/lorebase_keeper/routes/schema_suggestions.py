"""Schema suggestions from document extraction (design 9.2 step 10, 12.3 Review).

| Method and path | Permission | Notes |
|-----------------|------------|-------|
| `GET .../schema/suggestions` | `document.read` | `status`, `type`, `all` |
| `POST .../schema/suggestions:decide` | `schema.write` | accepting: see below |
| `POST .../schema/suggestions:merge` | `schema.write` | folds stored variants |

Suggestions quote their documents, so listing them needs `document.read` (Domain-wide). Pending
suggestions are listed once they reach `schema.suggestions.min_support` observations, the most
supported first, at most `schema.suggestions.max_suggestions` in all.

A decision rejects a suggestion, resets it to `pending`, or accepts it. All suggestions accepted
by one request become one schema change set, committed on a new branch
(`keeper.schema_suggestions.branch_prefix` and the first suggestion's id) and proposed into the
default branch: the answer names the Proposal, which reviewers merge as any other. Accepting
needs `schema.write` on that branch, `branch.create` and `proposal.create`; when the change set
is invalid the answer is 422 with the issues and nothing is decided. An accepted suggestion
cannot be decided again. Decisions are audited (`schema.suggestions.decide`).

Vocabulary suggestions (`kind_alias`, `attribute_alias`, `value_alias`,
`attribute_description`, design 8.10) come from Scribe's consolidation of the phrases
`lorebase_resolve` matched to nothing: `name` is the alias, `attribute` the Attribute changed,
`value` the stored value an alias stands for, `description` the proposed description, and the
examples are the missed phrases with their counts. They are listed and decided like the others;
one whose Kind the caller may not read Records of (`data.read` for that Kind) or whose
Attribute it may not read values of is listed `withheld`: without the alias (`name` is
`(withheld)`), phrases, value and description, and it cannot be accepted by that caller.

`:merge` folds the pending Attribute suggestions stored before variants were merged on arrival
into the best supported one of each set (`schema.suggestions.merge_similarity`); it is safe to
repeat and audited (`schema.suggestions.merge`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from lorebase_keeper.auth.audit import annotate
from lorebase_keeper.auth.deps import Access, AuthDep, PrincipalDep, authorize, require
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.auth.rbac import AccessContext
from lorebase_keeper.auth.runtime import AuthRuntime
from lorebase_keeper.context import StoreDep, require_domain
from lorebase_keeper.deps import KeeperSettingsDep
from lorebase_keeper.errors import Problem, ValidationProblem
from lorebase_keeper.pagination import Page, PageParamsDep, encode_cursor
from lorebase_keeper.reading.sensitivity import Masking, default_schema, masking_for
from lorebase_keeper.routes.schema import ElementChangeOut
from lorebase_keeper.services import schema_suggestions as service
from lorebase_keeper.services.errors import commit_problem
from lorebase_store.commit import CommitError

router = APIRouter(tags=["schema"])

SUGGESTIONS = "/domains/{domain_id}/schema/suggestions"
StatusName = Literal["pending", "accepted", "rejected"]
TypeName = Literal[
    "kind",
    "attribute",
    "relation",
    "kind_alias",
    "attribute_alias",
    "value_alias",
    "attribute_description",
]

_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": Problem, "description": "The caller lacks the permission."},
    404: {"model": Problem, "description": "Domain or suggestion not found."},
    409: {"model": Problem, "description": "A suggestion was accepted already."},
    422: {
        "model": ValidationProblem,
        "description": "Invalid request, a suggestion the schema has meanwhile, or accepted "
        "suggestions that do not form a valid schema.",
    },
    503: {"model": Problem, "description": "A branch is locked by another writer."},
}

Read = Annotated[Access, Depends(require(Permission.DOCUMENT_READ))]
WITHHELD = "(withheld)"
"""The name of a vocabulary suggestion listed to a caller who may not see its phrases."""


class TypeGuessOut(BaseModel):
    type: str = Field(description="Attribute type inferred from the samples, e.g. `date`.")
    constraints: dict[str, JsonValue]
    confidence: float
    reasons: list[str]


class DuplicateOut(BaseModel):
    section: Literal["kinds", "attributes", "relations"]
    api_name: str


class SuggestionOut(BaseModel):
    id: str
    type: TypeName
    name: str = Field(description="Proposed api name.")
    kind: str | None = Field(
        default=None,
        description="Owner Kind of an Attribute, from Kind of a Relation: an api name, or "
        "`+Name` for a Kind another suggestion proposes.",
    )
    target: str | None = Field(default=None, description="To Kind of a Relation.")
    attribute: str | None = Field(
        default=None, description="Vocabulary suggestions: the Attribute they change."
    )
    value: str | None = Field(
        default=None, description="Value aliases: the stored value the alias stands for."
    )
    description: str | None = Field(
        default=None, description="Attribute descriptions: the proposed description."
    )
    labels: list[str] = Field(
        description="Labels as the documents wrote them; the missed phrases of a vocabulary "
        "suggestion."
    )
    support: int = Field(
        description="Observations, over every document; misses for a vocabulary suggestion."
    )
    documents: int = Field(
        description="Documents it was observed in; distinct missed phrases for a vocabulary "
        "suggestion."
    )
    confidence: float = Field(description="Mean confidence of the observations.")
    status: StatusName
    type_guess: TypeGuessOut | None = Field(
        default=None, description="For Attributes: the type the samples suggest."
    )
    samples: list[str]
    examples: list[dict[str, Any]] = Field(
        description="Evidence: document, chunk, offsets into the parsed text, quote; for a "
        "vocabulary suggestion the missed phrase, its misses, distinct callers and the "
        "similarity that linked it."
    )
    sources: list[str] = Field(description="Techniques that observed it (rules, models).")
    duplicate_of: DuplicateOut | None = Field(
        default=None, description="The element of the current schema it duplicates."
    )
    withheld: bool = Field(
        default=False,
        description="Examples and samples are left out: it matches a sensitive Attribute the "
        "caller may not read.",
    )
    proposal_id: str | None
    branch: str | None
    decided_by: str | None
    decided_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None


class SuggestionDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suggestion: str = Field(min_length=1)
    decision: StatusName
    name: str | None = Field(
        default=None, max_length=100, description="Api name to use instead (accepted only)."
    )


class DecideSuggestionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: list[SuggestionDecisionIn] = Field(min_length=1)


class MergeSuggestionsOut(BaseModel):
    merged: int = Field(description="Suggestions folded into another and removed.")
    kept: int = Field(description="Suggestions that absorbed others.")


class SuggestionProposalOut(BaseModel):
    id: str
    title: str
    branch: str
    changes: list[ElementChangeOut]


class DecideSuggestionsOut(BaseModel):
    suggestions: list[SuggestionOut]
    proposal: SuggestionProposalOut | None = Field(
        default=None, description="The Proposal opened for the accepted suggestions."
    )


def _out(view: service.SuggestionView) -> SuggestionOut:
    row = view.row
    guess = view.guess
    value = row.payload.get("value")
    description = row.payload.get("description")
    vocabulary_withheld = view.withheld and view.vocabulary
    return SuggestionOut(
        id=row.id,
        type=row.suggestion_type,
        name=WITHHELD if vocabulary_withheld else row.name,
        kind=view.kind,
        target=view.target,
        attribute=view.attribute,
        value=value if isinstance(value, str) and not view.withheld else None,
        description=description if isinstance(description, str) and not view.withheld else None,
        labels=[] if vocabulary_withheld else list(row.labels),
        support=row.support,
        documents=row.documents,
        confidence=row.confidence,
        status=row.status,
        type_guess=None
        if guess is None
        else TypeGuessOut(
            type=guess.type.describe(),
            constraints=guess.constraints.model_dump(mode="json", exclude_none=True),
            confidence=guess.confidence,
            reasons=list(guess.reasons),
        ),
        samples=[] if view.withheld else list(row.samples),
        examples=[] if view.withheld else list(row.examples),
        sources=list(row.sources),
        duplicate_of=None
        if view.duplicate is None
        else DuplicateOut(
            section=view.duplicate.section,
            api_name=view.duplicate.api_name,
        ),
        withheld=view.withheld,
        proposal_id=row.proposal_id,
        branch=row.branch,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _masking(
    store: StoreDep, auth: AuthRuntime, principal: Principal, domain_id: str, settings: Any
) -> tuple[Masking, frozenset[str]]:
    """The caller's masking, and the ids of the Kinds whose Records it may read (`data.read`
    for the Kind): vocabulary suggestions of other Kinds are withheld."""
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        reference = await default_schema(session, domain_id, store.runtime)
    masking = await masking_for(auth.authorizer, principal, domain_id, reference, settings)
    readable: set[str] = set()
    for kind in [] if reference is None else reference.kinds.values():
        if await auth.authorizer.allows(
            principal, Permission.DATA_READ, domain_id, AccessContext(kind=kind.api_name)
        ):
            readable.add(kind.id)
    return (masking if reference is None else masking.over(reference)), frozenset(readable)


@router.get(SUGGESTIONS, summary="List schema suggestions", responses=_ERRORS)
async def list_suggestions(
    domain_id: str,
    access: Read,
    store: StoreDep,
    params: PageParamsDep,
    auth: AuthDep,
    keeper: KeeperSettingsDep,
    status: Annotated[list[StatusName] | None, Query()] = None,
    type_: Annotated[list[TypeName] | None, Query(alias="type")] = None,
    all_: Annotated[
        bool,
        Query(alias="all", description="Also those below `schema.suggestions.min_support`."),
    ] = False,
) -> Page[SuggestionOut]:
    """Kinds, Attributes and Relations extraction found outside the schema, with support,
    examples, samples and, for Attributes, the type their samples suggest."""
    masking, readable = await _masking(
        store, auth, access.principal, domain_id, keeper.reads.sensitivity
    )
    found = await service.page(
        store,
        domain_id,
        masking,
        readable=readable,
        statuses=status or [],
        types=type_ or [],
        all_support=all_,
        limit=params.limit,
        position=params.position,
    )
    following = None if found.next_position is None else encode_cursor(found.next_position)
    return Page[SuggestionOut](items=[_out(v) for v in found.views], next_cursor=following)


@router.post(
    SUGGESTIONS + ":decide", summary="Accept or reject schema suggestions", responses=_ERRORS
)
async def decide_suggestions(
    domain_id: str,
    body: DecideSuggestionsIn,
    request: Request,
    principal: PrincipalDep,
    auth: AuthDep,
    store: StoreDep,
    keeper: KeeperSettingsDep,
) -> DecideSuggestionsOut:
    """Reject or reset suggestions; accept some as one schema change Proposal."""
    settings = keeper.schema_suggestions
    accepting = [d for d in body.decisions if d.decision == "accepted"]
    if accepting:
        branch = f"{settings.branch_prefix}{accepting[0].suggestion}"
        for permission, context in (
            (Permission.BRANCH_CREATE, AccessContext()),
            (Permission.SCHEMA_WRITE, AccessContext(branch=branch)),
            (Permission.PROPOSAL_CREATE, AccessContext()),
        ):
            await authorize(request, auth, principal, permission, domain_id, context)
    else:
        await authorize(request, auth, principal, Permission.SCHEMA_WRITE, domain_id)
    masking, readable = await _masking(store, auth, principal, domain_id, keeper.reads.sensitivity)
    try:
        outcome = await service.decide(
            store,
            domain_id,
            [service.DecisionInput(d.suggestion, d.decision, d.name) for d in body.decisions],
            principal,
            settings=settings,
            branches=keeper.branches,
            proposals=keeper.proposals,
            masking=masking,
            readable=readable,
        )
    except CommitError as error:
        raise commit_problem(
            error,
            max_issues=keeper.schema_changes.max_issues,
            retry_after_s=keeper.branches.lock_retry_after_s,
        ) from None
    annotate(
        request,
        action="schema.suggestions.decide",
        target=domain_id,
        accepted=len(accepting),
        rejected=sum(1 for d in body.decisions if d.decision == "rejected"),
        reset=sum(1 for d in body.decisions if d.decision == "pending"),
        proposal=outcome.proposal_id,
    )
    proposal = None
    if outcome.proposal_id is not None and outcome.branch is not None:
        proposal = SuggestionProposalOut(
            id=outcome.proposal_id,
            title=outcome.proposal_title or "",
            branch=outcome.branch,
            changes=[
                ElementChangeOut.model_validate(c, from_attributes=True) for c in outcome.changes
            ],
        )
    return DecideSuggestionsOut(suggestions=[_out(v) for v in outcome.views], proposal=proposal)


@router.post(
    SUGGESTIONS + ":merge",
    summary="Merge stored variants of schema suggestions",
    responses={k: v for k, v in _ERRORS.items() if k in (403, 404)},
)
async def merge_suggestions(
    domain_id: str,
    request: Request,
    principal: PrincipalDep,
    auth: AuthDep,
    store: StoreDep,
) -> MergeSuggestionsOut:
    """Fold pending Attribute suggestions that are variants of one another, with the same
    similarity rules as new observations (idempotent)."""
    await authorize(request, auth, principal, Permission.SCHEMA_WRITE, domain_id)
    outcome = await service.merge_variants(store, domain_id)
    annotate(
        request,
        action="schema.suggestions.merge",
        target=domain_id,
        merged=outcome.merged,
        kept=outcome.kept,
    )
    return MergeSuggestionsOut(merged=outcome.merged, kept=outcome.kept)
