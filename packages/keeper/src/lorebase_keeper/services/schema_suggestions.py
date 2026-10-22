"""Review of schema suggestions (design 9.2 step 10).

Extraction jobs aggregate what they saw outside the schema into `schema_suggestions`
(`lorebase_scribe.extraction.discovery`). Reviewers list them, reject them, or accept a set of
them: the accepted suggestions become one schema change set (`suggestion_patch`) committed on a
new branch (`keeper.schema_suggestions.branch_prefix` and the first suggestion's id) and
proposed into the default branch as a Proposal. Nothing reaches the default branch until that
Proposal merges; new Kinds and Relations are drafts for a modeler to describe and activate.

Listings compare each suggestion with the default branch's schema of now: one that duplicates
an element meanwhile is flagged (`duplicate_of`) and cannot be accepted, and one that
duplicates a sensitive Attribute the caller may not read is listed without examples and
samples.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from lorebase_core.infer.suggestions import (
    PROPOSED,
    AttributeGuess,
    ExistingMatch,
    Suggestion,
    SuggestionError,
    SuggestionOptions,
    SuggestionType,
    existing_match,
    infer_attribute,
    sensitive_attributes,
    suggestion_patch,
)
from lorebase_core.model import Kind, Schema
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.context import StoreAccess, require_domain
from lorebase_keeper.errors import CONFLICT, NOT_FOUND, VALIDATION_FAILED, ProblemError
from lorebase_keeper.reading.sensitivity import Masking
from lorebase_keeper.services import branches as branch_service
from lorebase_keeper.services import proposals as proposal_service
from lorebase_keeper.services import schema as schema_service
from lorebase_keeper.services.config import (
    BranchSettings,
    ProposalSettings,
    SchemaSuggestionSettings,
)
from lorebase_keeper.services.errors import Finding, violations_problem
from lorebase_store.commit.schema import load_compiled
from lorebase_store.repositories import (
    MaterializedRepository,
    SchemaSuggestionRepository,
    SuggestionRow,
)
from lorebase_store.repositories.suggestions import ACCEPTED, PENDING, REJECTED

DECISIONS = (PENDING, ACCEPTED, REJECTED)
ISSUES_LISTED = 50


@dataclass(frozen=True)
class SuggestionView:
    """A stored suggestion as the caller may see it, against the current schema."""

    row: SuggestionRow
    kind: str | None
    target: str | None
    guess: AttributeGuess | None
    duplicate: ExistingMatch | None
    withheld: bool


@dataclass(frozen=True)
class SuggestionPage:
    views: list[SuggestionView]
    next_position: dict[str, Any] | None


@dataclass(frozen=True)
class DecisionInput:
    suggestion: str
    decision: str
    name: str | None = None


@dataclass
class DecisionOutcome:
    views: list[SuggestionView]
    proposal_id: str | None = None
    proposal_title: str | None = None
    branch: str | None = None
    changes: list[schema_service.ElementChange] = field(default_factory=list)


def invalid(pointer: str, message: str, **extra: Any) -> ProblemError:
    return ProblemError(
        VALIDATION_FAILED,
        "One or more request members are invalid.",
        extensions={"errors": [{"pointer": pointer, "message": message, **extra}]},
    )


async def default_schema(store: StoreAccess, domain_id: str) -> tuple[str, Schema]:
    """The default branch's name and schema."""
    async with store.sessions() as session, session.begin():
        domain = await require_domain(session, domain_id)
        compiled = await MaterializedRepository(
            session, domain_id, domain.default_branch, writes=store.runtime.settings.writes
        ).schema()
    if compiled is None:
        raise ProblemError(NOT_FOUND, f"Branch {domain.default_branch!r} is not materialized.")
    return domain.default_branch, load_compiled(compiled.compiled)


def _kind_label(schema: Schema, ref: str | None) -> str | None:
    if ref is None:
        return None
    if ref.startswith(PROPOSED):
        return ref
    kind = schema.kinds.get(ref)
    return ref if kind is None else kind.api_name


def view_of(
    row: SuggestionRow,
    schema: Schema,
    options: SuggestionOptions,
    masking: Masking | None,
) -> SuggestionView:
    """What a listing shows of `row` against `schema` for a caller with `masking`."""
    type_ = SuggestionType(row.suggestion_type)
    duplicate = existing_match(
        schema,
        type_,
        row.name,
        kind=row.kind_ref,
        target=row.target_ref,
        options=options,
        tags=sensitive_attributes(schema),
    )
    withheld = False
    if duplicate is not None and duplicate.sensitive:
        owner = schema.attribute_owner(duplicate.id)
        attribute = owner.attributes.get(duplicate.id) if isinstance(owner, Kind) else None
        kinds = (owner.id,) if isinstance(owner, Kind) else ()
        withheld = masking is None or attribute is None or masking.hidden(attribute, kinds)
    guess = None
    if type_ is SuggestionType.ATTRIBUTE and row.samples and not withheld:
        guess = infer_attribute(row.name, row.samples, options)
    return SuggestionView(
        row=row,
        kind=_kind_label(schema, row.kind_ref),
        target=_kind_label(schema, row.target_ref),
        guess=guess,
        duplicate=duplicate,
        withheld=withheld,
    )


async def page(
    store: StoreAccess,
    domain_id: str,
    masking: Masking | None,
    *,
    statuses: Sequence[str],
    types: Sequence[str],
    all_support: bool,
    limit: int,
    position: dict[str, Any] | None,
) -> SuggestionPage:
    """Suggestions by support, the most supported first: pending ones need
    `schema.suggestions.min_support` (unless `all_support`), and at most `max_suggestions`
    are listed in all."""
    options = store.runtime.schema.suggestions
    _, schema = await default_schema(store, domain_id)
    listed = 0
    after = None
    if position is not None:
        try:
            after = (int(position["support"]), str(position["id"]))
            listed = int(position["listed"])
        except (KeyError, TypeError, ValueError):
            raise ProblemError(
                VALIDATION_FAILED, "The cursor is not a suggestion cursor."
            ) from None
    room = options.max_suggestions - listed
    if room <= 0:
        return SuggestionPage([], None)
    take = min(limit, room)
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        rows = await SchemaSuggestionRepository(session, domain_id).page(
            statuses=statuses,
            types=types,
            min_support=1 if all_support else options.min_support,
            limit=take + 1,
            after=after,
        )
    more = len(rows) > take
    rows = rows[:take]
    following = None
    if more and rows and listed + len(rows) < options.max_suggestions:
        last = rows[-1]
        following = {"support": last.support, "id": last.id, "listed": listed + len(rows)}
    return SuggestionPage([view_of(r, schema, options, masking) for r in rows], following)


async def decide(
    store: StoreAccess,
    domain_id: str,
    decisions: Sequence[DecisionInput],
    principal: Principal,
    *,
    settings: SchemaSuggestionSettings,
    branches: BranchSettings,
    proposals: ProposalSettings,
    masking: Masking | None,
) -> DecisionOutcome:
    """Reject, reset or accept suggestions. Accepted ones are applied together on a new branch
    and proposed into the default branch; nothing is decided when that fails."""
    if len(decisions) > settings.max_decisions:
        raise invalid("/body/decisions", f"at most {settings.max_decisions} per request")
    ids = [d.suggestion for d in decisions]
    if len(set(ids)) != len(ids):
        raise invalid("/body/decisions", "a suggestion is named twice")
    options = store.runtime.schema.suggestions
    default_branch, schema = await default_schema(store, domain_id)
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        rows = await SchemaSuggestionRepository(session, domain_id).get_many(ids)
    for index, decision in enumerate(decisions):
        row = rows.get(decision.suggestion)
        if row is None:
            raise ProblemError(NOT_FOUND, f"Suggestion {decision.suggestion!r} not found.")
        if row.status == ACCEPTED:
            raise ProblemError(
                CONFLICT,
                f"Suggestion {row.id!r} was accepted into Proposal {row.proposal_id!r}.",
                extensions={"proposal": row.proposal_id},
            )
        if decision.name is not None and not decision.name.strip():
            raise invalid(f"/body/decisions/{index}/name", "must not be empty")
    accepted = [d for d in decisions if d.decision == ACCEPTED]
    outcome = DecisionOutcome(views=[])
    now = store.clock()
    if accepted:
        await _accept(
            store,
            domain_id,
            [(d, rows[d.suggestion]) for d in accepted],
            schema,
            default_branch,
            principal,
            options,
            settings,
            branches,
            proposals,
            outcome,
        )
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        repository = SchemaSuggestionRepository(session, domain_id)
        if accepted:
            await repository.decide(
                [d.suggestion for d in accepted],
                status=ACCEPTED,
                decided_by=principal.id,
                decided_at=now,
                proposal_id=outcome.proposal_id,
                branch=outcome.branch,
            )
        for status in (REJECTED, PENDING):
            chosen = [d.suggestion for d in decisions if d.decision == status]
            await repository.decide(
                chosen,
                status=status,
                decided_by=principal.id if status == REJECTED else None,
                decided_at=now if status == REJECTED else None,
            )
        stored = await repository.get_many(ids)
    outcome.views = [view_of(stored[i], schema, options, masking) for i in ids]
    return outcome


def _patch(
    rows: Sequence[tuple[DecisionInput, SuggestionRow]],
    schema: Schema,
    options: SuggestionOptions,
) -> dict[str, Any]:
    suggestions: list[Suggestion] = []
    for index, (decision, row) in enumerate(rows):
        type_ = SuggestionType(row.suggestion_type)
        duplicate = existing_match(
            schema, type_, row.name, kind=row.kind_ref, target=row.target_ref, options=options
        )
        if duplicate is not None:
            raise invalid(
                f"/body/decisions/{index}",
                f"the schema has {duplicate.api_name!r} ({duplicate.section}) already",
                suggestion=row.id,
            )
        suggestions.append(
            Suggestion(
                id=row.id,
                type=type_,
                name=decision.name.strip() if decision.name else row.name,
                kind=row.kind_ref,
                target=row.target_ref,
                samples=tuple(row.samples),
                labels=tuple(row.labels),
            )
        )
    try:
        return suggestion_patch(schema, suggestions, options)
    except SuggestionError as error:
        position = next(i for i, (_, row) in enumerate(rows) if row.id == error.suggestion)
        raise invalid(
            f"/body/decisions/{position}", str(error), suggestion=error.suggestion
        ) from None


def _issues_problem(issues: Sequence[Finding]) -> ProblemError:
    return violations_problem(issues, limit=ISSUES_LISTED)


async def _accept(
    store: StoreAccess,
    domain_id: str,
    rows: Sequence[tuple[DecisionInput, SuggestionRow]],
    schema: Schema,
    default_branch: str,
    principal: Principal,
    options: SuggestionOptions,
    settings: SchemaSuggestionSettings,
    branches: BranchSettings,
    proposals: ProposalSettings,
    outcome: DecisionOutcome,
) -> None:
    change = schema_service.ChangeSetInput(patch=_patch(rows, schema, options))
    try:
        target = schema_service.target_schema(schema, change)
    except schema_service.ChangeSetInvalidError as error:
        raise _issues_problem(error.issues) from None
    issues = schema_service.static_issues(target, store.runtime.schema.rules())
    if issues:
        raise _issues_problem(issues)
    branch = f"{settings.branch_prefix}{rows[0][1].id}"
    branch_service.check_ref_name(branch, branches, "/body/decisions")
    await branch_service.create(
        store,
        domain_id,
        branch,
        principal,
        branches,
        source=default_branch,
        revision=None,
        idempotency_key=None,
    )
    names = ", ".join(row.name for _, row in rows)
    try:
        applied = await schema_service.apply(
            store,
            domain_id,
            branch,
            change,
            principal,
            message=f"Schema suggestions: {names}",
            expected_head=None,
            dry_run=False,
            cascade=False,
        )
        created = await proposal_service.create(
            store,
            domain_id,
            proposal_service.NewProposal(
                source_ref=branch,
                target_ref=default_branch,
                title=f"Schema suggestions: {names}"[:200],
                description="Suggested by document extraction: "
                + ", ".join(f"{row.suggestion_type} {row.name} ({row.id})" for _, row in rows),
            ),
            principal,
            proposals,
            idempotency_key=None,
        )
    except schema_service.ChangeSetInvalidError as error:
        await _drop(store, domain_id, branch, branches)
        raise _issues_problem(error.issues) from None
    except Exception:
        await _drop(store, domain_id, branch, branches)
        raise
    outcome.branch = branch
    outcome.changes = applied.changes
    outcome.proposal_id = created.view.proposal.id
    outcome.proposal_title = created.view.proposal.title


async def _drop(store: StoreAccess, domain_id: str, branch: str, settings: BranchSettings) -> None:
    """Delete the branch of a change set that could not be proposed (best effort)."""
    try:
        await branch_service.delete(store, domain_id, branch, settings)
    except ProblemError:
        return
