"""Items of extraction Proposals (design 9.2 step 12, 12.3 Review: extraction items).

| Method and path | Permission | Notes |
|-----------------|------------|-------|
| `GET .../proposals/{p}/items` | `document.read` | filters `status`, `kind`, `type`, confidence |
| `POST .../proposals/{p}/items:decide` | `proposal.review` (target branch) | listed or by filter |

Items quote their documents, so listing them needs `document.read` (Domain-wide). An item
names its Kind, Relation and Attributes by api name and by id; `subject` keeps the ids.

A decision accepts, rejects or resets (`pending`) items, by id with optional reviewer `edits`,
or all items matching `where` with one `decision`. Edits by item type: `record` takes
`{"values": {attribute: value}}`, `value` takes `{"value": v, "key": k}` (`key` picks another
Record, for ambiguous matches), `link` takes `{"from": key, "to": key, "values": {...}}`.
After the decisions, the extraction branch is re-materialized to hold exactly the accepted
items (with `extraction` Lineage) and the Proposal's summary and merge preview are refreshed,
in one transaction: when the branch rejects the accepted set (a new Record missing a required
value or Link), the answer is 422 with the issues and nothing changes. Decisions are audited
(`proposal.items.decide`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from lorebase_core.model import Schema, Sensitivity
from lorebase_keeper.auth.audit import annotate
from lorebase_keeper.auth.deps import Access, AuthDep, PrincipalDep, authorize, require
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.auth.rbac import AccessContext
from lorebase_keeper.auth.runtime import AuthRuntime
from lorebase_keeper.context import StoreDep, require_domain
from lorebase_keeper.deps import KeeperSettingsDep
from lorebase_keeper.errors import (
    CONFLICT,
    NOT_FOUND,
    VALIDATION_FAILED,
    Problem,
    ProblemError,
    ValidationProblem,
)
from lorebase_keeper.pagination import Page, PageParamsDep, encode_cursor
from lorebase_keeper.reading.sensitivity import (
    Masking,
    default_schema,
    masking_for,
    relation_kinds,
)
from lorebase_keeper.services.errors import commit_problem
from lorebase_store.commit import CommitError
from lorebase_store.commit.schema import load_compiled
from lorebase_store.extraction import (
    EXTRACTION_CHECK,
    Decision,
    ReviewError,
    decide_items,
    extraction_proposal,
)
from lorebase_store.models.enums import ItemDecision
from lorebase_store.repositories import (
    ItemFilter,
    ItemRow,
    MaterializedRepository,
    ProposalItemRepository,
    ProposalRow,
)
from lorebase_store.repositories.proposal_items import ITEM_TYPES

router = APIRouter(tags=["proposals"])

ITEMS = "/domains/{domain_id}/proposals/{proposal_id}/items"
DecisionName = Literal["pending", "accepted", "rejected"]
ItemType = Literal["record", "value", "link"]

_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": Problem, "description": "The caller lacks the permission."},
    404: {"model": Problem, "description": "Domain or Proposal not found."},
    409: {"model": Problem, "description": "Not an extraction Proposal, or not open."},
    422: {
        "model": ValidationProblem,
        "description": "Invalid request, unknown items, invalid edits, or the branch rejects "
        "the accepted items.",
    },
    503: {"model": Problem, "description": "The extraction branch is locked by another writer."},
}

Read = Annotated[Access, Depends(require(Permission.DOCUMENT_READ))]


class ItemOut(BaseModel):
    id: str
    type: ItemType
    seq: int
    decision: DecisionName
    confidence: float | None
    tier: int | None
    model: str | None
    kind: str | None = Field(default=None, description="Kind api name (record, value items).")
    relation: str | None = Field(default=None, description="Relation api name (link items).")
    key: str | None = None
    attribute: str | None = Field(default=None, description="Attribute api name (value items).")
    value: JsonValue = None
    current: JsonValue = Field(default=None, description="The value on the target branch.")
    from_key: str | None = Field(default=None, alias="from")
    to_key: str | None = Field(default=None, alias="to")
    values: dict[str, JsonValue] = Field(
        default_factory=dict, description="Proposed values by Attribute api name, edits applied."
    )
    subject: dict[str, Any] = Field(description="What the item proposes, by ids, before edits.")
    evidence: dict[str, Any] | None
    match: dict[str, Any] | None
    edits: dict[str, Any] | None
    decided_by: str | None
    decided_at: datetime | None
    not_applied: str | None = Field(
        default=None, description="Why an accepted item is not on the branch."
    )

    model_config = ConfigDict(populate_by_name=True)


class ItemSelection(BaseModel):
    """Items matching every given filter."""

    model_config = ConfigDict(extra="forbid")

    status: list[DecisionName] = Field(default_factory=list)
    kind: list[str] = Field(
        default_factory=list, description="Kind or Relation api names (or ids)."
    )
    type: list[ItemType] = Field(default_factory=list)
    min_confidence: float | None = Field(default=None, ge=0, le=1)
    max_confidence: float | None = Field(
        default=None, ge=0, le=1.01, description="Exclusive upper bound."
    )


class ItemDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item: str = Field(min_length=1)
    decision: DecisionName
    edits: dict[str, Any] | None = Field(
        default=None, description="Reviewer edits; omitted keeps the stored ones, {} clears."
    )


class DecideIn(BaseModel):
    """Decisions by item, and or one decision for every item matching `where`."""

    model_config = ConfigDict(extra="forbid")

    decisions: list[ItemDecisionIn] = Field(default_factory=list)
    where: ItemSelection | None = None
    decision: DecisionName | None = Field(default=None, description="Decision for `where`.")

    @model_validator(mode="after")
    def _something(self) -> Self:
        if (self.where is None) != (self.decision is None):
            raise ValueError("`where` and `decision` go together")
        if not self.decisions and self.where is None:
            raise ValueError("give `decisions`, or `where` with `decision`")
        return self


class MaterializedOut(BaseModel):
    revision: str | None
    changes: int
    applied: int
    skipped: dict[str, str]


class DecideOut(BaseModel):
    proposal_id: str
    title: str
    checks: dict[str, Any] = Field(description="The `extraction` summary and `merge` preview.")
    items: list[ItemOut] = Field(description="The decided items.")
    materialized: MaterializedOut


def _names(schema: Schema) -> dict[str, str]:
    names: dict[str, str] = {}
    for kind in schema.kinds.values():
        names[kind.id] = kind.api_name
        names.update({a.id: a.api_name for a in kind.attributes.values()})
    for relation in schema.relations.values():
        names[relation.id] = relation.api_name
        names.update({a.id: a.api_name for a in relation.attributes.values()})
    return names


@dataclass(frozen=True)
class _Shown:
    """Which Attribute values of extraction items the caller may see."""

    hidden: frozenset[str] = frozenset()
    known: frozenset[str] | None = None
    """Attribute ids of the schema; None trusts every id (no masking)."""
    full: bool = True
    masked: str = "***"

    def masks(self, attribute_id: Any) -> bool:
        """Values of a hidden Attribute, or of one the schema does not know unless the caller
        is cleared for everything."""
        if attribute_id in self.hidden:
            return True
        return self.known is not None and attribute_id not in self.known and not self.full


def _shown(schema: Schema, masking: Masking) -> _Shown:
    hidden = {
        attribute.id
        for kind in schema.kinds.values()
        for attribute in kind.attributes.values()
        if masking.hidden(attribute, (kind.id,))
    }
    hidden |= {
        attribute.id
        for relation in schema.relations.values()
        for attribute in relation.attributes.values()
        if masking.hidden(attribute, relation_kinds(schema, relation))
    }
    known = {a.id for kind in schema.kinds.values() for a in kind.attributes.values()}
    known |= {a.id for relation in schema.relations.values() for a in relation.attributes.values()}
    return _Shown(
        frozenset(hidden),
        frozenset(known),
        masking.clearance.covers(Sensitivity, ()),
        masking.masked,
    )


def _scrub(document: Any, shown: _Shown, attribute: Any = None) -> Any:
    """An item's subject or edits with the values the caller may not see masked: entries of
    `values`, and `value` and `current` when the item is about such an Attribute."""
    if not isinstance(document, dict):
        return document
    out = dict(document)
    if isinstance(out.get("values"), dict):
        out["values"] = {
            k: (shown.masked if shown.masks(k) and v is not None else v)
            for k, v in out["values"].items()
        }
    about = attribute or out.get("attribute")
    if about is not None and shown.masks(about):
        for name in ("value", "current"):
            if out.get(name) is not None:
                out[name] = shown.masked
    return out


def _touches_masked(row: ItemRow, shown: _Shown) -> bool:
    """Whether an item is about a value the caller may not see: its evidence and match quote
    the document and candidate Records, so they are left out then."""
    subject = row.subject or {}
    keys = [subject.get("attribute"), *(subject.get("values") or {})]
    keys += list((row.edits or {}).get("values") or {})
    return any(key is not None and shown.masks(key) for key in keys)


def _item(
    row: ItemRow,
    names: dict[str, str],
    skipped: dict[str, Any],
    shown: _Shown | None = None,
) -> ItemOut:
    shown = shown or _Shown()
    subject = _scrub(row.subject, shown)
    edits = _scrub(row.edits or {}, shown, subject.get("attribute"))
    quoted = not _touches_masked(row, shown)
    values = {**(subject.get("values") or {}), **(edits.get("values") or {})}
    kind = subject.get("kind")
    relation = subject.get("relation")
    attribute = subject.get("attribute")
    return ItemOut(
        id=row.item_id,
        type=row.item_type,
        seq=row.seq,
        decision=row.decision.value,
        confidence=row.confidence,
        tier=row.tier,
        model=row.model,
        kind=None if kind is None else names.get(kind, kind),
        relation=None if relation is None else names.get(relation, relation),
        key=edits.get("key", subject.get("key")),
        attribute=None if attribute is None else names.get(attribute, attribute),
        value=edits.get("value", subject.get("value")),
        current=subject.get("current"),
        **{
            "from": edits.get("from", subject.get("from")),
            "to": edits.get("to", subject.get("to")),
        },
        values={names.get(k, k): v for k, v in values.items()},
        subject=subject,
        evidence=row.evidence if quoted else None,
        match=row.match if quoted else None,
        edits=edits if row.edits else row.edits,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        not_applied=(
            str(skipped[row.item_id])
            if row.decision is ItemDecision.ACCEPTED and row.item_id in skipped
            else None
        ),
    )


def _targets(names: list[str], schema: Schema) -> tuple[str, ...]:
    found: list[str] = []
    for name in names:
        kind = schema.kinds.get(name) or schema.kind_by_api_name(name)
        if kind is not None:
            found.append(kind.id)
            continue
        relation = schema.relations.get(name)
        by_name = schema.relation_by_api_name(name)
        if relation is None and by_name is not None:
            relation = by_name[0]
        if relation is None:
            raise ProblemError(
                VALIDATION_FAILED,
                "One or more request members are invalid.",
                extensions={
                    "errors": [
                        {"pointer": "/kind", "message": f"unknown Kind or Relation {name!r}"}
                    ]
                },
            )
        found.append(relation.id)
    return tuple(found)


def _filter(selection: ItemSelection, schema: Schema) -> ItemFilter:
    return ItemFilter(
        decisions=tuple(ItemDecision(s) for s in selection.status),
        targets=_targets(selection.kind, schema),
        item_types=tuple(t for t in selection.type if t in ITEM_TYPES),
        min_confidence=selection.min_confidence,
        max_confidence=selection.max_confidence,
    )


def _review_problem(error: ReviewError) -> ProblemError:
    if error.reason == "not_found":
        return ProblemError(NOT_FOUND, str(error))
    if error.reason in ("not_extraction", "not_open"):
        return ProblemError(CONFLICT, str(error))
    return ProblemError(
        VALIDATION_FAILED,
        str(error),
        extensions={
            "errors": [
                {"pointer": "/body/decisions", "message": str(error), "item": item}
                for item in (error.item_ids or [""])
            ]
        },
    )


async def _schema(store: StoreDep, domain_id: str, proposal: ProposalRow) -> Schema:
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        compiled = await MaterializedRepository(
            session, domain_id, proposal.target_ref, writes=store.runtime.settings.writes
        ).schema()
    if compiled is None:
        raise ProblemError(NOT_FOUND, f"Branch {proposal.target_ref!r} not found.")
    return load_compiled(compiled.compiled)


async def _masking(
    store: StoreDep, auth: AuthRuntime, principal: Principal, domain_id: str, settings: Any
) -> Masking:
    """The caller's masking in the Domain: items quote stored values (`current`)."""
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        reference = await default_schema(session, domain_id, store.runtime)
    return await masking_for(auth.authorizer, principal, domain_id, reference, settings)


async def _proposal(store: StoreDep, domain_id: str, proposal_id: str) -> ProposalRow:
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        try:
            return await extraction_proposal(session, domain_id, proposal_id)
        except ReviewError as error:
            raise _review_problem(error) from None


@router.get(ITEMS, summary="List the items of an extraction Proposal", responses=_ERRORS)
async def list_items(
    domain_id: str,
    proposal_id: str,
    access: Read,
    store: StoreDep,
    params: PageParamsDep,
    auth: AuthDep,
    keeper: KeeperSettingsDep,
    status: Annotated[list[DecisionName] | None, Query()] = None,
    kind: Annotated[
        list[str] | None, Query(description="Kind or Relation api names (or ids).")
    ] = None,
    type_: Annotated[list[ItemType] | None, Query(alias="type")] = None,
    min_confidence: Annotated[float | None, Query(ge=0, le=1)] = None,
    max_confidence: Annotated[float | None, Query(ge=0, le=1.01)] = None,
) -> Page[ItemOut]:
    """Items in proposal order (Records, values, Links), with evidence and match details."""
    proposal = await _proposal(store, domain_id, proposal_id)
    schema = await _schema(store, domain_id, proposal)
    filters = _filter(
        ItemSelection(
            status=status or [],
            kind=kind or [],
            type=type_ or [],
            min_confidence=min_confidence,
            max_confidence=max_confidence,
        ),
        schema,
    )
    after = None
    if params.position is not None:
        try:
            after = (int(params.position["seq"]), str(params.position["id"]))
        except (KeyError, ValueError, TypeError):
            raise ProblemError(VALIDATION_FAILED, "The cursor is not an item cursor.") from None
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        rows = await ProposalItemRepository(session, domain_id).page(
            proposal_id, filters, limit=params.limit + 1, after=after
        )
    more = len(rows) > params.limit
    rows = rows[: params.limit]
    names = _names(schema)
    skipped = ((proposal.checks or {}).get(EXTRACTION_CHECK) or {}).get("skipped") or {}
    next_cursor = (
        encode_cursor({"seq": rows[-1].seq, "id": rows[-1].item_id}) if more and rows else None
    )
    masking = (
        await _masking(store, auth, access.principal, domain_id, keeper.reads.sensitivity)
    ).over(schema)
    shown = _shown(schema, masking)
    items = [_item(r, names, skipped, shown) for r in rows]
    return Page[ItemOut](items=items, next_cursor=next_cursor)


@router.post(ITEMS + ":decide", summary="Accept or reject extraction items", responses=_ERRORS)
async def decide(
    domain_id: str,
    proposal_id: str,
    body: DecideIn,
    request: Request,
    principal: PrincipalDep,
    auth: AuthDep,
    store: StoreDep,
    keeper: KeeperSettingsDep,
) -> DecideOut:
    """Record decisions, re-materialize the extraction branch and refresh the Proposal."""
    proposal = await _proposal(store, domain_id, proposal_id)
    await authorize(
        request,
        auth,
        principal,
        Permission.PROPOSAL_REVIEW,
        domain_id,
        AccessContext(branch=proposal.target_ref),
    )
    schema = await _schema(store, domain_id, proposal)
    decisions = [Decision(d.item, ItemDecision(d.decision), d.edits) for d in body.decisions]
    listed = {d.item_id for d in decisions}
    if body.where is not None and body.decision is not None:
        async with store.sessions() as session, session.begin():
            await require_domain(session, domain_id)
            selected = await ProposalItemRepository(session, domain_id).all(
                proposal_id, _filter(body.where, schema)
            )
        decisions += [
            Decision(row.item_id, ItemDecision(body.decision))
            for row in selected
            if row.item_id not in listed
        ]
    limit = keeper.proposals.max_item_decisions
    if len(decisions) > limit:
        raise ProblemError(VALIDATION_FAILED, f"At most {limit} items per request.")
    try:
        async with store.sessions() as session, session.begin():
            reviewed = await decide_items(
                session,
                domain_id,
                proposal_id,
                decisions,
                actor=principal.id,
                runtime=store.runtime,
                clock=store.clock,
            )
    except ReviewError as error:
        raise _review_problem(error) from None
    except CommitError as error:
        raise commit_problem(
            error, max_issues=20, retry_after_s=keeper.branches.lock_retry_after_s
        ) from None
    outcome = reviewed.outcome
    annotate(
        request,
        action="proposal.items.decide",
        target=proposal_id,
        accepted=sum(1 for d in decisions if d.decision is ItemDecision.ACCEPTED),
        rejected=sum(1 for d in decisions if d.decision is ItemDecision.REJECTED),
        reset=sum(1 for d in decisions if d.decision is ItemDecision.PENDING),
        revision=None if outcome.revision is None else outcome.revision.hex(),
    )
    names = _names(schema)
    masking = (await _masking(store, auth, principal, domain_id, keeper.reads.sensitivity)).over(
        schema
    )
    shown = _shown(schema, masking)
    return DecideOut(
        proposal_id=reviewed.proposal.id,
        title=reviewed.proposal.title,
        checks=reviewed.proposal.checks or {},
        items=[_item(r, names, outcome.skipped, shown) for r in reviewed.items],
        materialized=MaterializedOut(**outcome.as_dict()),
    )
