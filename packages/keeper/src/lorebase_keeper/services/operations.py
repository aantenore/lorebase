"""Operation execution (design 10.1).

    resolve the Operation (stored, or built-in from the schema of the branch head)
    -> authorize (built-in: operation.execute; custom: operation.execute_custom and the
       Operation's own `permissions`; built-in imports holding values of vault Attributes
       also pii.import on the whole Domain, `services.pii_import`)
    -> expressions gate (only when `expressions_enabled` is off: preconditions are refused
       or, when settings allow, skipped with a warning; steps and expression review policies
       are refused)
    -> idempotency check (principal, key): same parameters replay, others conflict
    -> review policy (custom `always`, or any Operation on a protected branch unless settings
       allow direct commits): refused with a pointer to Proposals (`services.agent_writes`
       turns that refusal into a task branch and a Proposal for callers that name a task;
       such an execution runs with `reviewed_by_proposal`)
    -> take the branch lock, check the expected head
    -> bind parameters, compute edits (stored Operations with `expressions_enabled`: evaluate
       preconditions (false: 422 `precondition-failed` with the expression and its position),
       the review policy expression (true: 409 `review-required` as above) and the steps with
       `lorebase_core.expr`, loading from the materialized head the Records `record()` reads
       and the Links `links()` follows)
    -> dry run: commit inside a transaction that is rolled back; report edits and violations
    -> commit (imports in batches of whole units, a Record with its Links, one Revision
       each), then the invocation record, an `operation.invoke` audit entry and the outbox
       events of the Operation's side effects (`services.operation_events`; deferred to the
       merge of the Proposal with `reviewed_by_proposal`) in the transaction of the last
       Revision.

Every Revision has origin `operation` (or `import` for bulk imports) with the invocation id as
reference; every changed Attribute is attributed to a provenance entry naming the invocation,
the Operation and the principal. The parameters hash is kept in the invocation record and in
the audit entry. Values given a validity period (`valid` of `update_{kind}`) get an entry of
their own carrying it. A decision of the reconciliation queue (design 10.6) runs with
`ExecutionRequest.reconciliation`: its entries have that `reconciliation` origin instead, which
only Keeper builds; `within` lets it record the decision in the transaction of the write.
"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from pydantic import JsonValue
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.canonical import from_hex, hex
from lorebase_core.expr import (
    CompiledExpression,
    EvaluationContext,
    ExprError,
    ExprErrorCode,
    ExprLimits,
    LinkedRecord,
    LinksRequest,
    MemoryResolver,
    RecordNotLoaded,
    RecordRequest,
    check_preconditions,
    compile_operation,
    evaluate_review_policy,
    plan_steps,
)
from lorebase_core.expr import Principal as ExpressionPrincipal
from lorebase_core.expr.ast import parse
from lorebase_core.expr.errors import PathPart
from lorebase_core.expr.sensitivity import (
    attributes_named,
    expression_fields,
    operation_reads,
    with_tags,
)
from lorebase_core.model import (
    Attribute,
    CreateLinkStep,
    CreateRecordStep,
    IdPrefix,
    ImportOrigin,
    Kind,
    LinkDirection,
    Operation,
    OperationOrigin,
    OperationStep,
    ProvenanceEntry,
    ReconciliationOrigin,
    Relation,
    ReviewMode,
    Schema,
    UpdateRecordStep,
    new_id,
    new_ulid,
)
from lorebase_core.revision import Origin
from lorebase_core.validate import DataChange, RecordUpsert, operation_sensitivity_issues
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.auth.rbac import AccessContext
from lorebase_keeper.context import StoreAccess, author_of, require_branch, require_domain
from lorebase_keeper.errors import FORBIDDEN, NOT_FOUND, VALIDATION_FAILED, ProblemError
from lorebase_keeper.reading.sensitivity import (
    Clearance,
    Disclosure,
    Masking,
    default_schema,
    generalize_issues,
    reference_tags,
)
from lorebase_keeper.services.config import OperationSettings
from lorebase_keeper.services.errors import (
    EXPRESSIONS_NOT_ENABLED,
    IDEMPOTENCY_CONFLICT,
    REVIEW_REQUIRED,
    Finding,
    finding_documents,
    findings_of,
)
from lorebase_keeper.services.errors import (
    PRECONDITION_FAILED as PRECONDITION_PROBLEM,
)
from lorebase_keeper.services.operation_catalog import (
    Action,
    BuiltinOperation,
    Parameter,
    Period,
    RecordReader,
    batches,
    bind,
    builtin_definition,
    delete_keys,
    edit_document,
    import_rows,
    plan_edits,
)
from lorebase_keeper.services.operation_events import (
    counted,
    execution_fields,
    write_side_effects,
)
from lorebase_keeper.services.pii_import import import_rows_vault_attributes
from lorebase_store.auth import AuditRepository, NewAuditEntry
from lorebase_store.commit import (
    BranchNotMaterializedError,
    Change,
    StaleHeadError,
    ValidationFailedError,
    commit,
)
from lorebase_store.commit.schema import load_compiled
from lorebase_store.digests import DigestKeys
from lorebase_store.materialize.links import PathCapExceededError, path_links_of
from lorebase_store.models.enums import InvocationStatus
from lorebase_store.reads import Direction, head_schema, links_of_record
from lorebase_store.repositories import (
    InvocationRepository,
    InvocationRow,
    MaterializedRepository,
    RefRepository,
)

REVIEW_PROTECTED = "protected-branch"
REVIEW_POLICY = "review-policy"
"""Why an execution needs review: its branch is protected, or its review policy says so."""

INVOKE_ACTION = "operation.invoke"
INVOCATION_PREFIX = "inv"
PRECONDITIONS_SKIPPED = "operation.preconditions_skipped"
PRECONDITION_FAILED = "operation.precondition_failed"

Check = Callable[[Permission, AccessContext], Awaitable[None]]
Reveal = Callable[[AsyncSession], Awaitable[Masking]]
"""The caller's masking in the Domain (`reading.sensitivity`), read in the given session."""
LinkReader = Callable[[str, LinkDirection, str], Awaitable[Sequence[LinkedRecord]]]
"""Records linked to a key through a Relation id, traversed in a direction (`links()`)."""


class ExecutionStatus(StrEnum):
    COMMITTED = "committed"
    DRY_RUN = "dry_run"


@dataclass(frozen=True)
class ExecutionRequest:
    """What the caller asked for."""

    operation: str
    parameters: Mapping[str, JsonValue]
    branch: str | None = None
    dry_run: bool = False
    idempotency_key: str | None = None
    expected_head: bytes | None = None
    message: str | None = None
    reviewed_by_proposal: bool = False
    """The edits go to a task branch whose Proposal is their review (agent writes): the
    review policy and the protection of the branch no longer refuse the execution."""
    reconciliation: ReconciliationOrigin | None = None
    """Set by Keeper for a decision of the reconciliation queue, never from a request: the
    provenance entries of the execution have this origin (module doc)."""


Within = Callable[[AsyncSession, "ExecutionOutcome"], Awaitable[None]]
"""Called in the transaction of the last Revision, before it commits (not for dry runs)."""


@dataclass
class ExecutionOutcome:
    """Result of an execution or a dry run."""

    status: ExecutionStatus
    operation: str
    branch: str
    invocation_id: str
    parameters_hash: str
    head: bytes
    revisions: list[bytes] = field(default_factory=list)
    edits: list[dict[str, Any]] = field(default_factory=list)
    edit_count: int = 0
    violations: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    review_required: bool = False
    replayed: bool = False
    preconditions_failed: bool = False
    """A dry run found the violations in `violations` in failed preconditions."""
    parameters: dict[str, Any] = field(default_factory=dict)
    """The parameters as recorded (`recorded_parameters`): values that may be sensitive are
    masked."""


@dataclass(frozen=True)
class _Resolved:
    name: str
    custom: Operation | None
    builtin: BuiltinOperation | None


REDACTED = "***"
"""Stands for a parameter value that is not recorded."""


def recorded_parameters(
    schema: Schema,
    resolved: _Resolved,
    parameters: Mapping[str, JsonValue],
    sensitive: Callable[[Attribute], bool] = lambda a: a.is_sensitive,
) -> dict[str, JsonValue]:
    """Parameters as kept in the invocation record and Proposal descriptions. A value that
    may hold a value of a sensitive Attribute is replaced by `REDACTED`: those records are
    read without clearance and erasure does not reach them (design 13.5). Built-in Operations
    keep the values of non-sensitive Attributes; other parameters are kept only when nothing
    the Operation writes is sensitive."""
    owners: list[Kind | Relation | None]
    builtin = resolved.builtin
    if builtin is not None:
        owners = [builtin.kind or builtin.relation]
        if owners == [None]:
            owners = [*schema.kinds.values(), *schema.relations.values()]
    else:
        assert resolved.custom is not None
        owners = []
        for step in resolved.custom.steps:
            kind_id = getattr(step, "kind", None)
            relation_id = getattr(step, "relation", None)
            owners.append(
                schema.kinds.get(kind_id) if kind_id else schema.relations.get(relation_id or "")
            )
    if not any(
        owner is None or any(sensitive(a) for a in owner.attributes.values()) for owner in owners
    ):
        return dict(parameters)
    plain: set[str] = set()
    if builtin is not None and (builtin.kind or builtin.relation) is not None:
        owner = builtin.kind or builtin.relation
        assert owner is not None
        plain = {a.api_name for a in owner.attributes.values() if not sensitive(a)}
    return {name: value if name in plain else REDACTED for name, value in parameters.items()}


def parameters_hash(
    operation: str,
    branch: str,
    parameters: Mapping[str, JsonValue],
    *,
    domain_id: str,
    digests: DigestKeys,
) -> str:
    """Stable digest of an invocation's inputs (idempotency, provenance, audit), keyed with the
    installation's digest key (`lorebase_store.digests`, ready), so a stored digest does not let
    anyone confirm guessed parameter values."""
    return digests.digest(
        domain_id, "parameters", _parameters_payload(operation, branch, parameters)
    )


def parameters_hashes(
    operation: str,
    branch: str,
    parameters: Mapping[str, JsonValue],
    *,
    domain_id: str,
    digests: DigestKeys,
) -> frozenset[str]:
    """Every digest an earlier invocation of the same inputs may have stored: under the current
    key and the earlier token keys (`DigestKeys.all_digests`)."""
    payload = _parameters_payload(operation, branch, parameters)
    return digests.all_digests(domain_id, "parameters", payload)


def _parameters_payload(operation: str, branch: str, parameters: Mapping[str, JsonValue]) -> bytes:
    return json.dumps(
        {"operation": operation, "branch": branch, "parameters": parameters},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def resolve(schema: Schema, name: str) -> _Resolved:
    """A stored Operation of that api name, else a built-in; 404 otherwise."""
    custom = schema.operation_by_api_name(name)
    if custom is not None:
        return _Resolved(name, custom, None)
    builtin = builtin_definition(schema, name)
    if builtin is None:
        raise ProblemError(NOT_FOUND, f"No Operation {name!r} on this branch.")
    return _Resolved(name, None, builtin)


EXPRESSIONS_DISABLED = "expressions are disabled by configuration (schema.expressions_enabled)"


def expressions_gate(operation: Operation, settings: OperationSettings) -> list[Finding]:
    """Refuse what needs CEL unless expressions are enabled; returns warnings for skipped
    preconditions."""
    warnings: list[Finding] = []
    if settings.expressions_enabled:
        return warnings
    if operation.preconditions:
        if not settings.skip_expression_preconditions:
            raise ProblemError(
                EXPRESSIONS_NOT_ENABLED,
                f"Operation {operation.api_name!r} has preconditions written in CEL, and "
                f"{EXPRESSIONS_DISABLED}: nothing was evaluated or executed.",
                extensions={"operation": operation.api_name},
            )
        warnings.append(
            Finding(
                PRECONDITIONS_SKIPPED,
                f"{len(operation.preconditions)} precondition(s) skipped: {EXPRESSIONS_DISABLED}",
                ("preconditions",),
                severity="warning",
            )
        )
    if operation.steps or operation.review_policy.mode is ReviewMode.EXPRESSION:
        raise ProblemError(
            EXPRESSIONS_NOT_ENABLED,
            f"Operation {operation.api_name!r} computes its edits or review policy with CEL, "
            f"and {EXPRESSIONS_DISABLED}: nothing was executed.",
            extensions={"operation": operation.api_name},
        )
    return warnings


def review_required(resolved: _Resolved, *, protected: bool, settings: OperationSettings) -> bool:
    """Whether the edits must go through a Proposal instead of a direct commit."""
    if _policy_demands_review(resolved):
        return True
    return protected and not settings.direct_commit_on_protected


def _origin(resolved: _Resolved, invocation_id: str) -> Origin:
    return Origin(type="import" if _is_import(resolved) else "operation", ref=invocation_id)


def _is_import(resolved: _Resolved) -> bool:
    return resolved.builtin is not None and resolved.builtin.is_import


def _provenance(
    resolved: _Resolved,
    invocation_id: str,
    principal: Principal,
    at: Any,
    *,
    reconciliation: ReconciliationOrigin | None = None,
    period: Period = (None, None),
) -> ProvenanceEntry:
    origin: ImportOrigin | OperationOrigin | ReconciliationOrigin
    if reconciliation is not None:
        origin = reconciliation
    elif _is_import(resolved):
        origin = ImportOrigin(batch_id=invocation_id)
    else:
        origin = OperationOrigin(
            invocation_id=invocation_id,
            operation=resolved.name,
            operation_id=None if resolved.custom is None else resolved.custom.id,
            principal=principal.id,
        )
    return ProvenanceEntry(
        id=new_id(IdPrefix.PROVENANCE),
        origin=origin,
        actor=principal.id,
        at=at,
        valid_from=period[0],
        valid_to=period[1],
    )


def _attributed(
    changes: Sequence[DataChange],
    entry: ProvenanceEntry,
    periods: Mapping[str, ProvenanceEntry],
) -> list[Change]:
    """`changes` attributed to `entry`, and the values given a period to the entry of their
    period (`periods`, by Attribute id)."""
    if not periods:
        return [Change(c, provenance=entry.id) for c in changes]
    out: list[Change] = []
    for change in changes:
        if isinstance(change, RecordUpsert):
            mapping = {
                attribute_id: periods[attribute_id].id if attribute_id in periods else entry.id
                for attribute_id in change.record.values
            }
            out.append(Change(change, provenance=mapping))
        else:
            out.append(Change(change, provenance=entry.id))
    return out


def _permission(
    resolved: _Resolved, branch: str, schema: Schema, parameters: Mapping[str, JsonValue]
) -> tuple[Permission, AccessContext]:
    if resolved.custom is not None:
        return Permission.OPERATION_EXECUTE_CUSTOM, AccessContext(
            operation=resolved.name, branch=branch
        )
    builtin = resolved.builtin
    assert builtin is not None
    kind = None if builtin.kind is None else builtin.kind.api_name
    if builtin.action is Action.DELETE_RECORDS and isinstance(parameters.get("kind"), str):
        # A bulk delete is scoped to its Kind, as `delete_{kind}` is.
        named = schema.kind_by_api_name(str(parameters["kind"]))
        kind = None if named is None else named.api_name
    return Permission.OPERATION_EXECUTE, AccessContext(
        kind=kind, operation=resolved.name, branch=branch
    )


def _replay(
    found: InvocationRow,
    request: ExecutionRequest,
    branch: str,
    digest: str,
    accepted: frozenset[str] = frozenset(),
) -> ExecutionOutcome:
    params = found.params
    if params.get("parameters_hash") not in {digest, *accepted}:
        raise ProblemError(
            IDEMPOTENCY_CONFLICT,
            "This Idempotency-Key was used with another Operation, branch or parameters.",
            extensions={"invocation_id": found.id},
        )
    revisions = [from_hex(r) for r in params.get("revisions", [])]
    return ExecutionOutcome(
        status=ExecutionStatus.COMMITTED,
        operation=request.operation,
        branch=branch,
        invocation_id=found.id,
        parameters_hash=digest,
        head=from_hex(params["head"]),
        revisions=revisions,
        edit_count=int(params.get("edit_count", 0)),
        replayed=True,
    )


def rejected(
    findings: Sequence[Finding],
    *,
    limit: int,
    edits: Sequence[dict[str, Any]] = (),
    committed: Sequence[bytes] = (),
) -> ProblemError:
    """422 with the violations; `committed` lists Revisions of earlier import batches."""
    return ProblemError(
        VALIDATION_FAILED,
        f"{len(findings)} violation(s); the edits were not committed.",
        extensions={
            "status": "rejected",
            "issues": finding_documents(findings, limit),
            "issue_count": len(findings),
            "edits": list(edits),
            "committed_revisions": [hex(r) for r in committed],
        },
    )


async def execute(
    store: StoreAccess,
    domain_id: str,
    request: ExecutionRequest,
    principal: Principal,
    settings: OperationSettings,
    *,
    check: Check,
    proposals_path: str,
    reveal: Reveal | None = None,
    within: Within | None = None,
) -> ExecutionOutcome:
    """Run an Operation on a branch (default: the Domain's default branch). Edits in the
    outcome are masked for the caller's clearance (`reveal`; without it, as for a caller
    without clearances), with vault references resolved as reads resolve them; validation
    issues about values the caller may not read have a generic code (`generalize_issues`).

    Raises `ProblemError` for refusals and violations and the commit pipeline's `CommitError`s
    (`StaleHeadError`, `BranchLockTimeoutError`, ...), which the route maps.
    """
    started = time.monotonic()
    async with store.sessions() as session:
        await session.begin()
        try:
            return await _execute(
                session,
                store,
                domain_id,
                request,
                principal,
                settings,
                check,
                proposals_path,
                started,
                reveal,
                within,
            )
        finally:
            if session.in_transaction():
                await session.rollback()


async def _execute(
    session: AsyncSession,
    store: StoreAccess,
    domain_id: str,
    request: ExecutionRequest,
    principal: Principal,
    settings: OperationSettings,
    check: Check,
    proposals_path: str,
    started: float,
    reveal: Reveal | None,
    within: Within | None = None,
) -> ExecutionOutcome:
    domain = await require_domain(session, domain_id)
    branch = request.branch or domain.default_branch
    ref = await require_branch(session, domain_id, branch)
    writes = store.runtime.settings.writes
    rows = MaterializedRepository(session, domain_id, branch, writes=writes)
    compiled = await rows.schema()
    if compiled is None:
        raise BranchNotMaterializedError(f"branch {branch!r} has no materialized head")
    # From the schema cache (by head Revision): the same object comes back, which keeps the
    # effective tags of `reading.sensitivity` cached too.
    cached = await head_schema(session, domain_id, branch, runtime=store.runtime)
    schema = cached.schema if cached is not None else load_compiled(compiled.compiled)
    resolved = resolve(schema, request.operation)
    permission, context = _permission(resolved, branch, schema, request.parameters)
    await check(permission, context)
    if resolved.builtin is not None and import_rows_vault_attributes(
        schema, resolved.builtin, request.parameters
    ):
        # Imports check vault values against erased ones: refuse before anything is committed,
        # so the outcome does not tell whether a guessed value was erased.
        await check(Permission.PII_IMPORT, AccessContext())
    warnings: list[Finding] = []
    if resolved.custom is not None:
        _check_operation_permissions(resolved.custom, principal)
        warnings = expressions_gate(resolved.custom, settings)
    digests = await store.runtime.digests.ready(session)
    digest = parameters_hash(
        resolved.name, branch, request.parameters, domain_id=domain_id, digests=digests
    )
    accepted = parameters_hashes(
        resolved.name, branch, request.parameters, domain_id=domain_id, digests=digests
    )
    invocations = InvocationRepository(session, domain_id)
    if request.idempotency_key is not None and not request.dry_run:
        found = await invocations.by_key(principal.id, request.idempotency_key)
        if found is not None:
            return _replay(found, request, branch, digest, accepted)
    needs_review = (
        review_required(resolved, protected=ref.protected, settings=settings)
        and not request.reviewed_by_proposal
    )
    if needs_review and not request.dry_run:
        raise _review_problem(
            resolved.name,
            branch,
            proposals_path,
            REVIEW_POLICY if _policy_demands_review(resolved) else REVIEW_PROTECTED,
        )

    refs = RefRepository(session, domain_id)
    await refs.lock(branch, timeout_ms=store.runtime.settings.commit.lock_timeout_ms)
    ref = await require_branch(session, domain_id, branch)
    if request.expected_head is not None and request.expected_head != ref.head:
        raise StaleHeadError(branch, request.expected_head, ref.head)

    invocation_id = f"{INVOCATION_PREFIX}_{new_ulid()}"
    if reveal is not None:
        base = await reveal(session)
    else:
        base = execution_masking(None, await default_schema(session, domain_id, store.runtime))
    masking = base.over(schema)
    if resolved.custom is not None:
        # The Operation may have been valid when the branch forked, before the default branch
        # tagged what it reads: check it again with those tags.
        stale = operation_sensitivity_issues(
            with_tags(schema, masking.reference), resolved.custom, store.runtime.schema.rules()
        )
        if stale:
            raise rejected(findings_of(stale), limit=settings.max_issues)
    outcome = ExecutionOutcome(
        status=ExecutionStatus.DRY_RUN if request.dry_run else ExecutionStatus.COMMITTED,
        operation=resolved.name,
        branch=branch,
        invocation_id=invocation_id,
        parameters_hash=digest,
        head=ref.head,
        warnings=warnings,
        review_required=needs_review,
        parameters=recorded_parameters(schema, resolved, request.parameters, masking.sensitive),
    )
    read_links = _link_reader(session, store, domain_id, branch, schema, rows)
    planned = await _plan(
        schema,
        resolved,
        request,
        rows,
        read_links,
        settings,
        principal,
        store.clock(),
        redact=_redactor(schema, masking, resolved.custom, settings.expression_limits),
    )
    changes, findings = planned.changes, planned.findings
    outcome.warnings.extend(planned.warnings)
    if planned.review and not needs_review and not request.reviewed_by_proposal:
        outcome.review_required = True
        if not request.dry_run:
            raise _review_problem(resolved.name, branch, proposals_path, REVIEW_POLICY)
    outcome.edit_count = len(changes)
    outcome.edits = await _edits(
        Disclosure(session, domain_id, (schema,), masking, store.runtime.vault),
        schema,
        masking,
        changes[: settings.dry_run_max_edits],
    )
    if findings:
        if request.dry_run:
            outcome.violations = findings
            outcome.preconditions_failed = planned.preconditions_failed
            return outcome
        if planned.preconditions_failed:
            raise _precondition_problem(resolved.name, findings, limit=settings.max_issues)
        raise rejected(findings, limit=settings.max_issues, edits=outcome.edits)

    at = store.clock()
    entry = _provenance(
        resolved, invocation_id, principal, at, reconciliation=request.reconciliation
    )
    by_period: dict[Period, ProvenanceEntry] = {}
    for period in planned.periods.values():
        if period not in by_period:
            by_period[period] = _provenance(
                resolved,
                invocation_id,
                principal,
                at,
                reconciliation=request.reconciliation,
                period=period,
            )
    periods = {attribute: by_period[period] for attribute, period in planned.periods.items()}
    entries = [entry, *by_period.values()]
    origin = _origin(resolved, invocation_id)
    message = request.message or f"{resolved.name} ({invocation_id})"
    batch_size = settings.import_batch_rows if _is_import(resolved) else max(len(changes), 1)
    planned_batches = batches(changes, planned.units, batch_size) or [[]]
    head = ref.head
    for position, batch in enumerate(planned_batches):
        if position and not request.dry_run:
            await session.commit()
            await session.begin()
            await require_domain(session, domain_id)
            await refs.lock(branch, timeout_ms=store.runtime.settings.commit.lock_timeout_ms)
        try:
            result = await commit(
                session,
                domain_id,
                branch,
                _attributed(batch, entry, periods),
                author=author_of(principal),
                message=message,
                origin=origin,
                provenance=entries,
                expected_head=head,
                runtime=store.runtime,
                clock=store.clock,
            )
        except ValidationFailedError as error:
            issues = generalize_issues(error.issues, (schema,), masking)
            located = _locate(findings_of(issues), position, batch_size, resolved)
            if request.dry_run:
                outcome.violations = located
                return outcome
            raise rejected(
                located,
                limit=settings.max_issues,
                edits=outcome.edits,
                committed=outcome.revisions,
            ) from error
        if not result.no_op:
            outcome.revisions.append(result.revision_id)
            head = result.revision_id
    if request.dry_run:
        outcome.revisions = []
        return outcome
    events: list[int] = []
    try:
        await _record(
            session, domain_id, request, principal, outcome, resolved, store, started, head
        )
        if within is not None:
            await within(session, outcome)
        if resolved.custom is not None:
            events = await write_side_effects(
                session,
                domain_id,
                resolved.custom,
                schema=schema,
                masking=masking,
                rows=rows,
                changes=changes,
                execution=execution_fields(
                    branch=branch,
                    invocation_id=invocation_id,
                    principal=principal.id,
                    revisions=outcome.revisions,
                    head=head,
                    parameters=outcome.parameters,
                ),
                max_edits=settings.side_effect_max_edits,
                defer=request.reviewed_by_proposal,
            )
        await session.commit()
    except IntegrityError:
        # A concurrent request with the same idempotency key won: answer with its result.
        await session.rollback()
        async with store.sessions() as fresh, fresh.begin():
            await require_domain(fresh, domain_id)
            assert request.idempotency_key is not None
            found = await InvocationRepository(fresh, domain_id).by_key(
                principal.id, request.idempotency_key
            )
        if found is None:
            raise
        return _replay(found, request, branch, digest, accepted)
    counted(events)
    return outcome


async def _edits(
    disclosure: Disclosure, schema: Schema, masking: Masking, changes: Sequence[DataChange]
) -> list[dict[str, Any]]:
    """The edits shown for `changes`. An update carries the Record's current values: those of
    vault Attributes are references, shown as on every read (the value, `erased_value` or the
    mask). Only the whole value of a vault Attribute is resolved, and only when its vault row
    belongs to that Kind and Attribute; the edits are copies, so nothing resolved here reaches
    the changes that are committed."""
    edits = [copy.deepcopy(edit_document(schema, change, masking)) for change in changes]
    slots: list[tuple[Any, Any, str, str]] = []
    for change, document in zip(changes, edits, strict=True):
        if not isinstance(change, RecordUpsert):
            continue
        kind = schema.kinds.get(change.record.kind)
        values = document.get("values")
        if kind is None or not isinstance(values, dict):
            continue
        slots.extend(
            (values, attribute.api_name, kind.id, attribute.id)
            for attribute in kind.attributes.values()
            if attribute.in_vault and attribute.api_name in values
        )
    await disclosure.slots(slots)
    return edits


def _policy_demands_review(resolved: _Resolved) -> bool:
    return resolved.custom is not None and resolved.custom.review_policy.mode is ReviewMode.ALWAYS


def refused(dry_run: ExecutionOutcome, *, limit: int) -> ProblemError:
    """The 422 a dry run with violations stands for: what executing it would have raised."""
    if dry_run.preconditions_failed:
        return _precondition_problem(dry_run.operation, dry_run.violations, limit=limit)
    return rejected(dry_run.violations, limit=limit, edits=dry_run.edits)


def _review_problem(operation: str, branch: str, proposals_path: str, reason: str) -> ProblemError:
    return ProblemError(
        REVIEW_REQUIRED,
        f"Edits of {operation!r} on branch {branch!r} need review: run the Operation on "
        "another branch and open a Proposal.",
        extensions={
            "branch": branch,
            "operation": operation,
            "proposals": proposals_path,
            "reason": reason,
        },
    )


def _precondition_problem(
    operation: str, findings: Sequence[Finding], *, limit: int
) -> ProblemError:
    """422 naming each failed precondition with its expression and position."""
    return ProblemError(
        PRECONDITION_PROBLEM,
        f"{len(findings)} precondition(s) of {operation!r} failed; nothing was executed.",
        extensions={
            "status": "rejected",
            "operation": operation,
            "issues": finding_documents(findings, limit),
            "issue_count": len(findings),
        },
    )


def _link_reader(
    session: AsyncSession,
    store: StoreAccess,
    domain_id: str,
    branch: str,
    schema: Schema,
    rows: MaterializedRepository,
) -> LinkReader:
    """`links()` for expressions: Links of a Record (stored or foreign-key backed, or of a
    path Derivation, composed from the Links of its steps) at the materialized head, with
    the Records at their far end."""
    settings = store.runtime.settings
    cap = settings.reads.max_page_rows

    def too_many() -> ExprError:
        return ExprError(
            ExprErrorCode.LIMIT_EXCEEDED,
            f"links() reached {cap} Links (store.reads.max_page_rows)",
        )

    async def read(relation_id: str, direction: LinkDirection, key: str) -> list[LinkedRecord]:
        path = schema.path_relations().get(relation_id)
        if path is not None:
            try:
                found_far = await path_links_of(
                    rows, path, direction, [key], settings.traverse.path_fanout, cap=cap
                )
            except PathCapExceededError:
                raise too_many() from None
            pairs = found_far.get(key, [])
            if len(pairs) >= cap:
                raise too_many()
            far_kind = path.relation.far_endpoint(direction)
            linked = await rows.records(far_kind, [far for far, _ in pairs])
            return [LinkedRecord(far, linked[far].inputs, {}) for far, _ in pairs if far in linked]
        relation = schema.relations.get(relation_id)
        if relation is None:
            return []
        forward = direction is LinkDirection.FORWARD
        found = await links_of_record(
            session,
            domain_id,
            branch,
            schema,
            relation.endpoint(direction),
            key,
            direction=Direction.OUT if forward else Direction.IN,
            relation_ids=[relation_id],
            limit=cap,
            runtime=store.runtime,
        )
        if len(found) >= cap:
            raise too_many()
        far = {(link.to_key if forward else link.from_key): link for link in found}
        records = await rows.records(relation.far_endpoint(direction), list(far))
        return [
            LinkedRecord(far_key, records[far_key].inputs, link.values)
            for far_key, link in far.items()
            if far_key in records
        ]

    return read


def _check_operation_permissions(operation: Operation, principal: Principal) -> None:
    """A stored Operation's `permissions` restrict it to principals or groups listed there."""
    if not operation.permissions or principal.method == "none":
        return
    if set(operation.permissions) & set(principal.subjects):
        return
    raise ProblemError(
        FORBIDDEN,
        f"Operation {operation.api_name!r} is restricted to {', '.join(operation.permissions)}.",
        extensions={"operation": operation.api_name},
    )


async def _plan(
    schema: Schema,
    resolved: _Resolved,
    request: ExecutionRequest,
    rows: MaterializedRepository,
    read_links: LinkReader,
    settings: OperationSettings,
    principal: Principal,
    now: datetime,
    *,
    redact: Redact | None = None,
) -> PlannedEdits:
    async def read(kind_id: str, keys: Sequence[str]) -> dict[str, Mapping[str, JsonValue]]:
        found = await rows.records(kind_id, keys)
        return {key: row.values for key, row in found.items()}

    async def read_inputs(kind_id: str, keys: Sequence[str]) -> dict[str, Mapping[str, JsonValue]]:
        """Stored and derived values: what `record()` returns to expressions."""
        found = await rows.records(kind_id, keys)
        return {key: row.inputs for key, row in found.items()}

    builtin = resolved.builtin
    if builtin is None:
        custom = resolved.custom
        if custom is None or not settings.expressions_enabled:
            return PlannedEdits()  # without expressions a stored Operation edits nothing
        return await plan_custom(
            schema,
            custom,
            request.parameters,
            read_inputs,
            principal,
            now,
            settings,
            read_links=read_links,
            redact=redact,
        )
    bound = bind(builtin, request.parameters)
    if bound.findings:
        return PlannedEdits(findings=bound.findings)
    if builtin.is_import:
        plan = import_rows(schema, builtin, bound.values, max_rows=settings.max_bulk_rows)
    elif builtin.action is Action.DELETE_RECORDS:
        plan = await delete_keys(schema, bound.values, read, max_keys=settings.max_bulk_delete_keys)
    else:
        plan = await plan_edits(builtin, bound.values, read)
    return PlannedEdits(
        changes=plan.changes,
        findings=plan.findings,
        units=plan.units,
        warnings=plan.warnings,
        periods=plan.periods,
    )


# Stored Operations with expressions ------------------------------------------------------------


@dataclass
class PlannedEdits:
    """Edits of an execution, or the findings preventing them; `review` is true when an
    expression review policy sends the edits to a Proposal, `preconditions_failed` when the
    findings are the preconditions that did not hold; `units` are the runs of changes
    committed together (see `operation_catalog.Plan`)."""

    changes: list[DataChange] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    review: bool = False
    preconditions_failed: bool = False
    units: list[int] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    periods: dict[str, Period] = field(default_factory=dict)
    """Validity periods of values, by Attribute id (`operation_catalog.Plan.periods`)."""


def _false_precondition(program: CompiledExpression, index: int, message: str) -> Finding:
    """A precondition that evaluated to false: its message, expression and position."""
    position = program.root.pos
    return Finding(
        PRECONDITION_FAILED,
        message,
        ("preconditions", index),
        details={
            "expression": program.source,
            "line": position.line,
            "column": position.column,
        },
    )


def execution_masking(revealed: Masking | None, reference: Schema | None) -> Masking:
    """The masking of an execution: the caller's (`reveal`), or without it (agent writes) no
    clearance, the default branch's tags still deciding what is sensitive."""
    if revealed is not None:
        return revealed
    return Masking(Clearance(), reference=MappingProxyType(reference_tags(reference)))


Redact = Callable[[ExprError], bool]
"""Whether an evaluation error must not be shown as it is (it may quote a value)."""

REDACTED_ERROR = "Evaluating the expression failed on a value the caller may not read."


def _redactor(
    schema: Schema,
    masking: Masking,
    operation: Operation | None = None,
    limits: ExprLimits | None = None,
) -> Redact:
    """Errors of expressions that read values the caller is not cleared for are redacted: their
    messages may quote the values (a cast, a missing Record's key). The caller must be cleared
    for each Attribute the expression may read on the Kinds that hold it (both Kinds of a
    Relation for a Link Attribute), as when the values are read.

    An error of a step may quote a value the step computed (one that does not fit its
    Attribute, the key of a Record it creates): it is redacted when any expression of the
    step reads such an Attribute, or when the Attribute written is one. An expression that
    cannot be parsed is redacted, since what it reads cannot be told."""
    holders = _attribute_kinds(schema)
    limits = limits or ExprLimits()

    def hidden(attributes: Iterable[Attribute]) -> bool:
        return any(masking.hidden(a, holders.get(a.id, ())) for a in attributes)

    def reads(source: str, step: int | None) -> list[Attribute] | None:
        """What `source` may read; `None` when it cannot be parsed."""
        try:
            parse(source, limits)
        except ExprError:
            return None
        if operation is None:
            return attributes_named(schema, expression_fields(source, limits))
        return operation_reads(schema, operation, source, limits, step=step)

    def redact(error: ExprError) -> bool:
        step = _step_of(error)
        sources = [] if error.source is None else [error.source]
        written: list[Attribute] = []
        if operation is not None and step is not None and step < len(operation.steps):
            sources += _step_sources(operation.steps[step])
            written = _step_writes(schema, operation.steps[step], tuple(error.path))
        for source in sources:
            read = reads(source, step)
            if read is None or hidden(read):
                return True
        return hidden(written)

    return redact


def _step_sources(step: OperationStep) -> list[str]:
    """Every expression of an Operation step."""
    sources = [getattr(step, name, None) for name in ("target", "from_", "to")]
    found = [s for s in sources if isinstance(s, str)]
    found += list(getattr(step, "values", {}).values())
    return found


def _step_writes(
    schema: Schema, step: OperationStep, path: tuple[PathPart, ...]
) -> list[Attribute]:
    """The Attributes an error of `step` at `path` may quote a written value of: the one at
    `("steps", n, "values", attribute)`, otherwise every Attribute the step writes."""
    if isinstance(step, CreateRecordStep | UpdateRecordStep):
        kind = schema.kinds.get(step.kind)
        attributes = {} if kind is None else kind.attributes
    elif isinstance(step, CreateLinkStep):
        relation = schema.relations.get(step.relation)
        attributes = {} if relation is None else relation.attributes
    else:
        return []
    if len(path) >= 4 and path[2] == "values":
        attribute = attributes.get(str(path[3]))
        return [] if attribute is None else [attribute]
    return [attributes[a] for a in step.values if a in attributes]


def _attribute_kinds(schema: Schema) -> dict[str, tuple[str, ...]]:
    """Attribute id -> ids of the Kinds whose Records (or Links) hold its values."""
    holders: dict[str, tuple[str, ...]] = {}
    for kind in schema.kinds.values():
        for attribute_id in kind.attributes:
            holders[attribute_id] = (kind.id,)
    for relation in schema.relations.values():
        for attribute_id in relation.attributes:
            holders[attribute_id] = (relation.from_kind, relation.to_kind)
    return holders


def _step_of(error: ExprError) -> int | None:
    """The step a located evaluation error comes from (`("steps", n, ...)`), if any."""
    path = tuple(error.path)
    if len(path) >= 2 and path[0] == "steps" and isinstance(path[1], int):
        return path[1]
    return None


def _expression_finding(error: ExprError, redact: Redact | None = None) -> Finding:
    if redact is not None and redact(error):
        return Finding(error.code.value, REDACTED_ERROR, tuple(error.path))
    details: dict[str, Any] = {}
    if error.source is not None:
        details["expression"] = error.source
    if error.position is not None:
        details["line"] = error.position.line
        details["column"] = error.position.column
    return Finding(error.code.value, error.describe(), tuple(error.path), details=details)


async def _with_records[T](
    compute: Callable[[], T],
    resolver: MemoryResolver,
    read: RecordReader,
    settings: OperationSettings,
    read_links: LinkReader | None = None,
) -> T:
    """Run a pure evaluation, loading each Record (or Link list) it asks for, then run it
    again; evaluation is deterministic, so a retry computes the same result."""
    for _ in range(settings.expression_limits.max_record_lookups + 1):
        try:
            return compute()
        except RecordNotLoaded as missing:
            request = missing.request
            if isinstance(request, RecordRequest):
                found = await read(request.kind, [request.key])
                resolver.records[(request.kind, request.key)] = found.get(request.key)
                continue
            assert isinstance(request, LinksRequest)
            if read_links is None:
                raise ExprError(
                    ExprErrorCode.EVALUATION, "links() cannot be read in this evaluation"
                ) from None
            resolver.links[(request.relation, request.direction, request.key)] = await read_links(
                request.relation, request.direction, request.key
            )
    raise ExprError(
        ExprErrorCode.LIMIT_EXCEEDED,
        f"more than {settings.expression_limits.max_record_lookups} Record lookups",
    )


async def plan_custom(
    schema: Schema,
    operation: Operation,
    parameters: Mapping[str, JsonValue],
    read: RecordReader,
    principal: Principal,
    now: datetime,
    settings: OperationSettings,
    *,
    mint_key: Callable[[], str] = new_ulid,
    read_links: LinkReader | None = None,
    redact: Redact | None = None,
) -> PlannedEdits:
    """Bind parameters, check preconditions, evaluate the review policy and plan the steps
    of a stored Operation with the expression engine (pure apart from `read` and
    `read_links`)."""
    definition = BuiltinOperation(  # only the parameters matter to `bind`
        operation.api_name,
        Action.CREATE,
        operation.description,
        tuple(Parameter(p.name, p.type, p.required, p.description) for p in operation.parameters),
    )
    bound = bind(definition, parameters)
    if bound.findings:
        return PlannedEdits(findings=bound.findings)
    resolver = MemoryResolver()
    context = EvaluationContext(
        now=now,
        resolver=resolver,
        principal=ExpressionPrincipal(principal.id, principal.subjects),
        limits=settings.expression_limits,
        mint_key=mint_key,
    )
    try:
        compiled = compile_operation(schema, operation, settings.expression_limits)
        failures = await _with_records(
            lambda: check_preconditions(compiled, bound.values, context),
            resolver,
            read,
            settings,
            read_links,
        )
        if failures:
            return PlannedEdits(
                findings=[
                    _expression_finding(f.error, redact)
                    if f.error is not None
                    else _false_precondition(compiled.preconditions[f.index], f.index, f.message)
                    for f in failures
                ],
                preconditions_failed=True,
            )
        review = await _with_records(
            lambda: evaluate_review_policy(compiled, bound.values, context),
            resolver,
            read,
            settings,
            read_links,
        )
        steps = await _with_records(
            lambda: plan_steps(compiled, bound.values, context),
            resolver,
            read,
            settings,
            read_links,
        )
    except ExprError as error:
        return PlannedEdits(findings=[_expression_finding(error, redact)])
    return PlannedEdits(changes=steps.changes, review=review)


def _locate(
    findings: list[Finding], batch: int, batch_size: int, resolved: _Resolved
) -> list[Finding]:
    """Findings of an import batch keep their Record keys; the batch is noted in details."""
    if not _is_import(resolved):
        return findings
    return [
        Finding(
            f.code,
            f.message,
            f.path,
            f.severity,
            f.kind,
            f.relation,
            f.key,
            f.attribute,
            {**f.details, "batch": batch, "batch_size": batch_size},
        )
        for f in findings
    ]


async def _record(
    session: AsyncSession,
    domain_id: str,
    request: ExecutionRequest,
    principal: Principal,
    outcome: ExecutionOutcome,
    resolved: _Resolved,
    store: StoreAccess,
    started: float,
    head: bytes,
) -> None:
    """Invocation record and audit entry, in the transaction of the last Revision."""
    revisions = [hex(r) for r in outcome.revisions]
    await InvocationRepository(session, domain_id).insert(
        InvocationRow(
            id=outcome.invocation_id,
            operation_id=resolved.custom.id if resolved.custom else resolved.name,
            principal=principal.id,
            params={
                "operation": resolved.name,
                "branch": outcome.branch,
                "parameters": outcome.parameters,
                "parameters_hash": outcome.parameters_hash,
                "head": hex(outcome.head),
                "revisions": revisions,
                "edit_count": outcome.edit_count,
            },
            status=InvocationStatus.COMMITTED,
            revision_id=head,
            proposal_id=None,
            idempotency_key=request.idempotency_key,
            created_at=store.clock(),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    )
    await AuditRepository(session).append(
        NewAuditEntry(
            domain_id=domain_id,
            actor=principal.id,
            action=INVOKE_ACTION,
            target=f"{outcome.branch}@{hex(head)}",
            details={
                "invocation_id": outcome.invocation_id,
                "operation": resolved.name,
                "parameters_hash": outcome.parameters_hash,
                "revisions": revisions,
                "edits": outcome.edit_count,
            },
        )
    )
