"""Typed request and response models of the Keeper API.

They mirror the routes in `packages/keeper` and the OpenAPI document in `docs/openapi.json`
(a contract test compares the two). Response models ignore members they do not know, so a
newer server does not break an older client. Values of Records and Links are in canonical
form: integers and decimals are JSON strings, timestamps RFC 3339.

Request models are validated when built; the clients send them without unset members.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

DOMAIN_ID_PATTERN = r"^[a-z][a-z0-9_-]*$"

SchemaSection = Literal["kinds", "relations", "traits", "operations", "derivations", "sources"]
SearchMode = Literal["lexical", "semantic", "hybrid"]
BranchMode = Literal["copy", "overlay"]
ServedFrom = Literal["materialized", "tree"]
ChangeKind = Literal["created", "updated", "deleted"]
LinkDirection = Literal["forward", "inverse"]
ProposalStatus = Literal["open", "merged", "closed"]
ProposalOrigin = Literal["manual", "source_sync", "extraction", "operation"]
ReviewVerdict = Literal["approve", "request_changes", "comment"]
ConflictStatus = Literal["open", "resolved"]
CheckStatus = Literal["passed", "failed", "pending"]
ContextFormat = Literal["json", "markdown"]
ItemDecisionValue = Literal["pending", "accepted", "rejected"]
ItemType = Literal["record", "value", "link"]


class Model(BaseModel):
    """Base of response models: immutable, unknown members ignored."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True)


class RequestModel(BaseModel):
    """Base of request models: unknown members are rejected before anything is sent."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def payload(self) -> dict[str, Any]:
        """JSON body: set members only, by alias."""
        return self.model_dump(mode="json", exclude_none=True, by_alias=True)


# Pages ------------------------------------------------------------------------------------


class Paged[T](Protocol):
    """Anything with `items` and `next_cursor`: what the pagination helpers walk."""

    @property
    def items(self) -> list[T]: ...

    @property
    def next_cursor(self) -> str | None: ...


class Page[T](Model):
    """One page of a list endpoint; `next_cursor` is null on the last page."""

    items: list[T]
    next_cursor: str | None = None


# Identity ---------------------------------------------------------------------------------


class QuotaUse(Model):
    """Use of one daily quota; without a limit, `limit` and `remaining` are None."""

    limit: int | None = None
    used: int
    remaining: int | None = None


class QuotaReport(Model):
    """Daily quotas of an API key and what it has used since the last reset."""

    period: str = "day"
    resets_at: datetime
    requests: QuotaUse
    context_tokens: QuotaUse


class WhoAmI(Model):
    """The authenticated caller."""

    id: str
    type: str
    display_name: str
    method: str
    subjects: list[str]
    key_scopes: list[str]
    installation_admin: bool
    quota: QuotaReport | None = None


# Domains ----------------------------------------------------------------------------------


class DomainCreate(RequestModel):
    """A new Domain."""

    id: str = Field(pattern=DOMAIN_ID_PATTERN, max_length=63)
    display_name: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=4000)
    default_branch: str | None = Field(default=None, min_length=1, max_length=200)
    settings: dict[str, JsonValue] = Field(default_factory=dict)


class DomainUpdate(RequestModel):
    """Display metadata; omitted members stay as they are."""

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)


class Domain(Model):
    """A Domain and the head Revision of its default branch."""

    id: str
    display_name: str
    description: str | None = None
    default_branch: str
    settings: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    created_by: str
    head: str | None = Field(default=None, description="Head of the default branch (get only).")


# Branches and tags ------------------------------------------------------------------------


class BranchCreate(RequestModel):
    """A new branch, started at a Revision or at the head of a branch or tag."""

    name: str = Field(min_length=1)
    source: str | None = None
    revision: str | None = None
    mode: BranchMode | None = None
    """`copy` (own rows) or `overlay` (only its changes); default: the server's setting."""


class BranchUpdate(RequestModel):
    """Branch flags."""

    protected: bool


class Branch(Model):
    """A branch."""

    name: str
    head: str
    protected: bool
    default: bool
    materialization: str
    version: int
    created_at: datetime
    created_by: str


class BranchDetail(Branch):
    """A branch with its distance to the default branch."""

    ahead: int
    behind: int


class TagCreate(RequestModel):
    """A new tag at a Revision or at the head of a branch."""

    name: str = Field(min_length=1)
    source: str | None = None
    revision: str | None = None


class Tag(Model):
    """An immutable tag."""

    name: str
    revision: str
    created_at: datetime
    created_by: str


# Schema -----------------------------------------------------------------------------------


class ElementRemoval(RequestModel):
    """A schema element to remove."""

    section: SchemaSection
    name: str = Field(min_length=1)


class _ChangeSet(RequestModel):
    yaml: str | None = Field(default=None, description="Full schema document.")
    patch: dict[str, JsonValue] | None = Field(default=None, description="Partial document.")
    remove: list[ElementRemoval] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_form(self) -> Self:
        if (self.yaml is not None) == (self.patch is not None or bool(self.remove)):
            raise ValueError("give either `yaml`, or `patch` and/or `remove`")
        return self


class SchemaChangeRequest(_ChangeSet):
    """A schema change set for one branch."""

    message: str | None = Field(default=None, max_length=2000)
    expected_head: str | None = None
    dry_run: bool = False
    cascade: bool = False


class SchemaValidateRequest(_ChangeSet):
    """A change set to validate against a branch or Revision."""

    branch: str | None = None
    at: str | None = None


class SchemaDocument(Model):
    """The schema of a Revision: elements keyed by id, with api names."""

    domain: str
    branch: str | None = None
    revision: str
    schema_: dict[str, Any] = Field(alias="schema")


class ElementChange(Model):
    """One schema element written or removed."""

    op: Literal["create", "update", "delete"]
    section: str
    id: str
    api_name: str


class SchemaChangeResult(Model):
    """Outcome of a schema change set."""

    status: Literal["committed", "dry_run", "no_op"]
    branch: str
    head: str
    revision: str | None = None
    changes: list[ElementChange]
    change_count: int
    data_changes: int
    issues: list[dict[str, Any]]
    issue_count: int


class SchemaValidation(Model):
    """Static validation of a change set."""

    valid: bool
    issues: list[dict[str, Any]]
    issue_count: int
    changes: list[ElementChange]


# Operations -------------------------------------------------------------------------------


class ExecuteRequest(RequestModel):
    """An execution of an Operation."""

    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    branch: str | None = None
    dry_run: bool = False
    idempotency_key: str | None = None
    expected_head: str | None = None
    message: str | None = Field(default=None, max_length=2000)
    task: str | None = Field(default=None, min_length=1, max_length=200)
    intent: str | None = Field(default=None, max_length=4000)
    auto_propose: bool | None = None
    auto_merge: bool | None = None


class Execution(Model):
    """Result of an Operation execution."""

    status: Literal["committed", "dry_run"]
    operation: str
    branch: str
    invocation_id: str
    parameters_hash: str
    head: str
    revision: str | None = None
    revisions: list[str]
    edits: list[dict[str, Any]]
    edit_count: int
    edits_truncated: bool
    violations: list[dict[str, Any]]
    violation_count: int
    warnings: list[dict[str, Any]]
    review_required: bool
    replayed: bool


class ProposalRef(Model):
    """The Proposal carrying the edits of an agent write."""

    id: str
    title: str
    status: str
    source_ref: str
    target_ref: str
    url: str
    created: bool


class AgentApproval(Model):
    required: int
    approvals: int
    satisfied: bool


class AgentAttempt(Model):
    """Conflicts between the task branch and the target."""

    id: str
    result: str | None = None
    open_conflicts: int
    resolved_conflicts: int
    counts: dict[str, dict[str, int]]


class AgentMerge(Model):
    requested: bool
    allowed: bool
    performed: bool
    blockers: list[str]


class NextHint(Model):
    """A next step: what to do, and the call that does it when there is one."""

    action: str
    detail: str
    method: str | None = None
    href: str | None = None
    permission: str | None = None


class AgentWrite(Model):
    """An Operation that needed review, run on a task branch and proposed (HTTP 202)."""

    status: Literal["proposed", "merged", "conflicts"]
    operation: str
    branch: str
    target: str
    invocation_id: str
    parameters_hash: str
    revision: str | None = None
    revisions: list[str]
    edit_count: int
    replayed: bool
    proposal: ProposalRef
    approval: AgentApproval
    attempt: AgentAttempt | None = None
    merge: AgentMerge
    merged_revision: str | None = None
    branch_created: bool
    branch_deleted: bool
    warnings: list[dict[str, Any]]
    next: list[NextHint]


# Reads ------------------------------------------------------------------------------------


class LinkCount(Model):
    relation: str
    direction: LinkDirection
    count: int


class Lineage(Model):
    provenance_id: str
    origin_type: str | None = None
    actor: str | None = None
    at: datetime | None = None


class RecordItem(Model):
    """A Record with values keyed by Attribute api name."""

    kind: str
    key: str
    title: str | None = None
    values: dict[str, JsonValue] = Field(default_factory=dict)
    updated_revision: str | None = None
    updated_at: datetime | None = None
    links: list[LinkCount] | None = None
    lineage: dict[str, Lineage] | None = None
    included: dict[str, list[RecordItem]] | None = None


class RecordRef(Model):
    kind: str
    key: str
    title: str | None = None


class Served(Model):
    """Where a response was read."""

    revision: str
    served_from: ServedFrom


class RecordResponse(Served):
    record: RecordItem


class RecordPage(Served):
    items: list[RecordItem]
    next_cursor: str | None = None


class AggregateCount(Model):
    kind: str
    value: JsonValue = None
    count: int


class Aggregate(Model):
    counts: list[AggregateCount]
    truncated: bool = False


class QueryResult(RecordPage):
    aggregate: Aggregate | None = None


class LinkItem(Model):
    """A Link seen from one side."""

    relation: str
    direction: LinkDirection
    from_key: str
    to_key: str
    values: dict[str, JsonValue] = Field(default_factory=dict)
    target: RecordRef | None = None


class LinkPage(Served):
    items: list[LinkItem]
    next_cursor: str | None = None


class Edge(Model):
    relation: str
    source: RecordRef = Field(alias="from")
    to: RecordRef
    values: dict[str, JsonValue] = Field(default_factory=dict)


class TraverseHop(Model):
    relation: str
    records: list[RecordItem]
    edges: list[Edge]
    truncated: bool = False


class TraverseResult(Served):
    start: RecordItem
    hops: list[TraverseHop]


class NeighborhoodResult(Served):
    nodes: list[RecordItem]
    edges: list[Edge]
    truncated: bool = False


class SearchHit(Model):
    kind: str
    key: str
    title: str | None = None
    snippet: str | None = None
    score: float


class SearchResult(Served):
    mode: str
    items: list[SearchHit]


class QueryRequest(RequestModel):
    """A query of the JSON DSL and where to run it. Predicates are plain JSON objects."""

    kind: str | None = None
    trait: str | None = None
    where: dict[str, JsonValue] | None = None
    select: list[str] | None = None
    include: dict[str, dict[str, JsonValue]] | None = None
    order_by: list[dict[str, str]] | None = None
    limit: int | None = Field(default=None, ge=0)
    cursor: str | None = None
    aggregate: dict[str, JsonValue] | None = None
    branch: str | None = None
    at: str | None = None


class RecordAddress(RequestModel):
    kind: str = Field(min_length=1)
    key: str = Field(min_length=1)


class TraverseStep(RequestModel):
    relation: str = Field(min_length=1)
    where: dict[str, JsonValue] | None = None
    limit: int | None = Field(default=None, ge=1)


class TraverseRequest(RequestModel):
    """Follow a path of Relations from one Record."""

    branch: str | None = None
    start: RecordAddress
    path: list[TraverseStep] = Field(min_length=1)


class NeighborhoodRequest(RequestModel):
    """Breadth-first expansion from seed Records."""

    branch: str | None = None
    seeds: list[RecordAddress] = Field(min_length=1)
    depth: int = Field(default=1, ge=1)
    relations: list[str] | None = None
    max_nodes: int | None = Field(default=None, ge=1)
    max_edges: int | None = Field(default=None, ge=1)


class SearchRequest(RequestModel):
    """Lexical, semantic (caller-supplied vector) or hybrid search."""

    branch: str | None = None
    q: str | None = Field(default=None, min_length=1)
    kinds: list[str] | None = None
    where: dict[str, JsonValue] | None = None
    mode: SearchMode | None = None
    query_vector: list[float] | None = None
    limit: int | None = Field(default=None, ge=1)


# Revisions, log and diff ------------------------------------------------------------------


class Revision(Model):
    id: str
    parents: list[str]
    root: str
    generation: int
    author: str
    on_behalf_of: str | None = None
    message: str
    origin: dict[str, Any]
    stats: dict[str, Any]
    created_at: datetime


class LogPage(Model):
    branch: str
    head: str
    items: list[Revision]
    next_cursor: str | None = None


class PartitionCount(Model):
    partition: str
    element: str
    name: str
    count: int


class RevisionCounts(Model):
    revision: str
    partitions: list[PartitionCount]


class ValueChange(Model):
    name: str
    old: JsonValue = None
    new: JsonValue = None


class Change(Model):
    """One changed element between two Revisions."""

    element: str
    change: Literal["added", "removed", "modified"]
    partition: str
    kind: str | None = None
    relation: str | None = None
    key: str | None = None
    from_key: str | None = None
    to_key: str | None = None
    element_type: str | None = None
    api_name: str | None = None
    old_hash: str | None = None
    new_hash: str | None = None
    changes: list[ValueChange] | None = None
    provenance: dict[str, str] | None = None


class PartitionSummary(Model):
    partition: str
    element: str
    name: str
    added: int = 0
    removed: int = 0
    modified: int = 0


class ChangesPage(Model):
    """A page of element changes with the summary of all of them."""

    from_revision: str | None = None
    to_revision: str
    total: int
    summary: list[PartitionSummary]
    items: list[Change]
    next_cursor: str | None = None


# Administration ---------------------------------------------------------------------------


class CreateApiKey(RequestModel):
    """Request to create an API key for a Domain."""

    name: str = Field(min_length=1, max_length=120)
    role: str | None = None
    ttl_days: int | None = Field(default=None, ge=1)
    principal_id: str | None = None
    allowed_networks: list[str] | None = None


class ApiKey(Model):
    """An API key; the secret is shown only when it is created."""

    id: str
    principal_id: str
    prefix: str
    scopes: list[str]
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime
    allowed_networks: list[str] = Field(default_factory=list)


class ApiKeyCreated(ApiKey):
    """A new API key: `key` is the secret, returned once."""

    key: str
    role: str | None = None


class Principal(Model):
    id: str
    type: str
    display_name: str
    external_subject: str | None = None
    created_at: datetime


class CreateGrant(RequestModel):
    """Request to grant a role."""

    subject: str = Field(min_length=1)
    role: str
    scope_type: Literal["domain", "kind", "operation", "branch"] = "domain"
    scope_ref: str | None = Field(default=None, min_length=1)


class Grant(Model):
    id: str
    domain_id: str
    subject: str
    role: str
    scope_type: str
    scope_ref: str | None = None
    created_by: str
    created_at: datetime


class AuditEntry(Model):
    seq: int
    at: datetime
    domain_id: str | None = None
    actor: str
    action: str
    target: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    entry_hash: str


# Proposals --------------------------------------------------------------------------------


class ProposalCreate(RequestModel):
    """A new Proposal: merge `source_ref` into `target_ref`."""

    source_ref: str = Field(min_length=1, max_length=200)
    target_ref: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=20000)


class ProposalUpdate(RequestModel):
    """Title and description; omitted members are left unchanged."""

    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=20000)


class ReviewIn(RequestModel):
    """A review of a Proposal."""

    verdict: ReviewVerdict
    comment: str | None = Field(default=None, max_length=20000)


class MergeIn(RequestModel):
    """Options of a merge."""

    message: str | None = Field(default=None, max_length=2000)


class ResolutionIn(RequestModel):
    """A decision for one Conflict (`value` or per-member `values` for `custom` only)."""

    choice: str
    value: JsonValue = None
    values: dict[str, JsonValue] | None = None


class FingerprintedResolutionIn(ResolutionIn):
    """A decision for the Conflict with this fingerprint."""

    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class ResolveManyIn(RequestModel):
    """Decisions for several Conflicts at once."""

    resolutions: list[FingerprintedResolutionIn]


class Check(Model):
    """One Proposal check."""

    name: str
    status: CheckStatus
    message: str | None = None


class Review(Model):
    id: str
    reviewer: str
    verdict: ReviewVerdict
    comment: str | None
    at: datetime


class Approval(Model):
    """Review state against the protection of the target branch."""

    protected: bool
    required: int
    approvals: int
    approvers: list[str]
    changes_requested: list[str]
    satisfied: bool


class Resolution(Model):
    """A stored resolution; `matched` is false when its Conflict is gone from the attempt."""

    fingerprint: str
    choice: str
    value: JsonValue = None
    values: dict[str, JsonValue] | None = None
    resolver: str
    at: datetime
    matched: bool = True


class Attempt(Model):
    """A merge attempt and the resolutions of its Proposal."""

    id: str
    proposal: str
    status: str
    result: str | None
    base: str | None
    ours: str
    theirs: str
    virtual_base: bool
    created_at: datetime
    created_by: str | None
    revision: str | None
    policy: dict[str, Any]
    open_conflicts: int
    resolved_conflicts: int
    counts: dict[str, dict[str, int]]
    recomputed: bool
    stale: bool
    resolutions: list[Resolution]
    unmatched_resolutions: list[str]
    auto_resolutions: list[dict[str, Any]]


class ConflictItem(Model):
    """A Conflict: `id`, `status` and the members the merge engine reports."""

    model_config = ConfigDict(extra="allow")

    id: str
    status: ConflictStatus
    resolution: dict[str, Any] | None = None


class Conflicts(Attempt):
    """An attempt with one page of its Conflicts; walkable with the pagination helpers."""

    conflicts: list[ConflictItem]
    next_cursor: str | None = None

    @property
    def items(self) -> list[ConflictItem]:
        return self.conflicts


class Proposal(Model):
    """A Proposal as listed."""

    id: str
    title: str
    description: str | None
    source_ref: str
    target_ref: str
    origin: ProposalOrigin
    status: ProposalStatus
    author: str
    created_at: datetime
    updated_at: datetime | None
    merged_revision: str | None
    policy: dict[str, Any]
    checks: list[Check]


class ProposalDetail(Proposal):
    """A Proposal with its reviews, approval state and last merge attempt."""

    reviews: list[Review]
    approval: Approval
    attempt: Attempt | None


class ReviewResult(Model):
    review: Review
    approval: Approval


class MergeResult(Model):
    """A merged Proposal."""

    status: Literal["merged"] = "merged"
    result: str
    revision: str
    proposal: Proposal
    attempt: Conflicts
    source_deleted: bool
    replayed: bool


# Agents -----------------------------------------------------------------------------------


class ContextRequest(RequestModel):
    """What an agent needs to reason about: seed Records and how much around them."""

    branch: str | None = None
    refs: list[str] | None = None
    query: dict[str, JsonValue] | None = None
    search: str | None = Field(default=None, min_length=1)
    budget_tokens: int | None = Field(default=None, ge=1)
    depth: int | None = Field(default=None, ge=0)
    relations: list[str] | None = None
    include_schema: bool = True
    include_recent_changes: bool = True
    include_lineage: bool = True
    since: str | None = None
    format: ContextFormat = "json"


class ContextPack(Model):
    """A context pack in JSON; sections are kept as plain data."""

    domain: str
    branch: str
    revision: str
    etag: str
    seeds: list[dict[str, Any]]
    neighbors: list[dict[str, Any]]
    links: dict[str, list[dict[str, Any]]]
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")
    recent_changes: list[dict[str, Any]] | None = None
    lineage: list[dict[str, Any]] | None = None
    since: str | None = None
    budget: dict[str, Any]


class Explain(Model):
    """An explained Record: where its values and Links came from; sections are plain data."""

    domain: str
    branch: str
    revision: str
    record: dict[str, Any]
    created: dict[str, Any] | None = None
    attributes: list[dict[str, Any]]
    links: list[dict[str, Any]]
    links_truncated: bool
    history: dict[str, Any]
    proposals: list[dict[str, Any]]


class Provenance(Model):
    """Where a change came from."""

    id: str
    origin_type: str
    origin: dict[str, JsonValue]
    actor: str
    at: datetime


class AttributeChange(Model):
    name: str
    before: JsonValue = None
    after: JsonValue = None
    provenance: Provenance | None = None


class RecordChange(Model):
    """One change of a Record in one Revision."""

    revision: str
    generation: int
    at: datetime
    author: str
    on_behalf_of: str | None = None
    message: str
    origin: dict[str, JsonValue]
    change: ChangeKind
    attributes: list[AttributeChange]
    provenance: Provenance | None = None


class RecordHistory(Served):
    branch: str
    kind: str
    key: str
    items: list[RecordChange]
    next_cursor: str | None = None


class LineageProposal(Model):
    id: str
    title: str
    status: str
    merged_revision: str | None = None


class ExtractionEvidence(Model):
    job_id: str
    document_id: str
    span_start: int
    span_end: int
    model_id: str
    prompt_version: str


class ValueLineage(Model):
    """The current value of an Attribute and the change behind it."""

    name: str
    value: JsonValue = None
    provenance: Provenance | None = None
    revision: str | None = None
    revision_at: datetime | None = None
    author: str | None = None
    message: str | None = None
    source: str | None = None
    operation: str | None = None
    extraction: ExtractionEvidence | None = None
    proposal: LineageProposal | None = None


class RecordLineage(Served):
    branch: str
    kind: str
    key: str
    created: RecordChange | None = None
    changes: int
    attributes: list[ValueLineage]
    sources: list[str]
    proposals: list[LineageProposal]


class HealthSummary(Model):
    """Totals of a health report; `ok` is false when there are violations, orphan Links,
    required-relation gaps or duplicates of unique Attributes."""

    ok: bool
    kinds: int
    records: int
    links: int
    invalid_records: int
    orphan_links: int
    required_relation_gaps: int
    duplicate_groups: int
    stale_kinds: int
    sampled_kinds: int


class DomainHealth(Model):
    """Health of a Domain at the head of a branch; Kind and Relation sections are plain data."""

    revision: str
    served_from: ServedFrom
    domain: str
    branch: str
    summary: HealthSummary
    kinds: list[dict[str, Any]]
    relations: list[dict[str, Any]]


class KindHealthReport(Model):
    """Health of one Kind with the Relations it takes part in."""

    revision: str
    served_from: ServedFrom
    domain: str
    branch: str
    summary: HealthSummary
    health: dict[str, Any]
    relations: list[dict[str, Any]]


class RevisionEvent(Model):
    """A Revision committed on a branch."""

    sequence: int | None = None
    type: str
    domain: str
    branch: str
    revision: str
    parents: list[str]
    generation: int
    author: str
    on_behalf_of: str | None = None
    message: str
    origin: dict[str, JsonValue]
    stats: dict[str, JsonValue]
    created_at: datetime
    kinds: dict[str, int]
    relations: dict[str, int]
    schema_changed: bool
    keys: dict[str, list[str]]
    keys_truncated: bool


class ChangesFeed(Model):
    """Revision events after a point; `next_since` continues the feed while `more` is true."""

    branch: str
    head: str
    since: str | None = None
    items: list[RevisionEvent]
    next_since: str
    more: bool


# Webhooks ---------------------------------------------------------------------------------


class CreateWebhook(RequestModel):
    """A webhook for events of a Domain, optionally narrowed to some branches."""

    url: str = Field(min_length=1, max_length=2048)
    events: list[str] | None = None
    branches: list[str] | None = None
    description: str | None = Field(default=None, max_length=500)


class Webhook(Model):
    id: str
    url: str
    description: str | None = None
    events: list[str]
    branches: list[str] | None = None
    created_by: str
    created_at: datetime


class WebhookCreated(Webhook):
    """A new webhook: `secret` signs deliveries and is returned once."""

    secret: str


class WebhookList(Model):
    items: list[Webhook]


# Sources ----------------------------------------------------------------------------------


class SyncRequest(RequestModel):
    """A synchronization of a Source; a dry run records the delta without writing."""

    dry_run: bool = False


class SyncQueued(Model):
    """The queued (or already pending) synchronization job."""

    job_id: int
    source: str
    api_name: str
    dry_run: bool


class SourceRun(Model):
    id: str
    source: str
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    stats: dict[str, JsonValue]
    revision: str | None = None
    proposal_id: str | None = None
    error: dict[str, JsonValue] | None = None


# Documents --------------------------------------------------------------------------------


class DocumentFromUrl(RequestModel):
    """A document fetched by the server from an allowlisted URL."""

    url: str = Field(min_length=1, max_length=2000)
    filename: str | None = Field(default=None, max_length=255)
    extract: bool | None = None
    tiers: list[int] | None = None


class Document(Model):
    """A stored document; `text_sha256` is set once Scribe parsed its text."""

    id: str
    filename: str
    media_type: str
    sha256: str
    size: int
    text_sha256: str | None = None
    metadata: dict[str, JsonValue]
    created_at: datetime | None = None
    created_by: str


class ExtractionJob(Model):
    id: str
    status: str
    model: str
    prompt_version: str
    branch: str | None = None
    proposal_id: str | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None
    usage: dict[str, JsonValue]
    stats: dict[str, JsonValue]
    error: dict[str, JsonValue] | None = None


class DocumentDetail(Document):
    """A document with its extraction jobs, newest first."""

    extraction_jobs: list[ExtractionJob]


class DocumentText(Model):
    """The parsed text of a document; extraction evidence offsets index it."""

    document_id: str
    text_sha256: str
    text: str
    length: int
    truncated: bool


class DocumentUploaded(Model):
    """The stored document; `duplicate` when the same bytes were stored before."""

    document: Document
    duplicate: bool
    job_id: int | None = None


# Extraction items -------------------------------------------------------------------------


class ProposalItem(Model):
    """One extracted Record, value or Link of an extraction Proposal."""

    id: str
    type: ItemType
    seq: int
    decision: ItemDecisionValue
    confidence: float | None
    tier: int | None
    model: str | None
    kind: str | None = None
    relation: str | None = None
    key: str | None = None
    attribute: str | None = None
    value: JsonValue = None
    current: JsonValue = None
    from_key: str | None = Field(default=None, alias="from")
    to_key: str | None = Field(default=None, alias="to")
    values: dict[str, JsonValue] = Field(default_factory=dict)
    subject: dict[str, JsonValue]
    evidence: dict[str, JsonValue] | None
    match: dict[str, JsonValue] | None
    edits: dict[str, JsonValue] | None
    decided_by: str | None
    decided_at: datetime | None
    not_applied: str | None = None


class ItemSelection(RequestModel):
    """Items matching every given filter."""

    status: list[ItemDecisionValue] | None = None
    kind: list[str] | None = None
    type: list[ItemType] | None = None
    min_confidence: float | None = Field(default=None, ge=0, le=1)
    max_confidence: float | None = Field(default=None, ge=0, le=1.01)


class ItemDecision(RequestModel):
    """A decision on one item; `edits` omitted keeps the stored ones, `{}` clears them."""

    item: str = Field(min_length=1)
    decision: ItemDecisionValue
    edits: dict[str, JsonValue] | None = None


class DecideItems(RequestModel):
    """Decisions by item, and or one decision for every item matching `where`."""

    decisions: list[ItemDecision] = Field(default_factory=list)
    where: ItemSelection | None = None
    decision: ItemDecisionValue | None = None


class MaterializedItems(Model):
    """What the extraction branch holds after the decisions."""

    revision: str | None
    changes: int
    applied: int
    skipped: dict[str, str]


class ItemsDecided(Model):
    proposal_id: str
    title: str
    checks: dict[str, JsonValue]
    items: list[ProposalItem]
    materialized: MaterializedItems


RecordItem.model_rebuild()
