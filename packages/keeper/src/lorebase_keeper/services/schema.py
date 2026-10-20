"""Schema reads and schema change sets (design 5, 6.6).

A change set is either a full schema document (YAML, the layout of
`lorebase_core.model.yaml_schema`) or a patch: a partial document whose elements replace the
elements of the same api name in the current schema, plus a list of elements to remove. Both
are turned into the target `Schema` (ids of existing elements are kept: an explicit `id` wins,
otherwise the element with the same api name keeps its id), validated statically, and diffed
against the current schema into `SchemaUpsert` and `SchemaDelete` changes that the commit
pipeline applies as one Revision, data migrations included.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import (
    Schema,
    SchemaYamlError,
    load_schema,
    locate_issues,
    schema_from_document,
    schema_to_document,
    validate_schema,
)
from lorebase_core.revision import Origin
from lorebase_core.validate import Issue
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.context import (
    StoreAccess,
    author_of,
    require_branch,
    require_domain,
    require_revision,
)
from lorebase_keeper.services.errors import Finding, ProtectedBranchError, findings_of
from lorebase_keeper.services.reads import ReadTarget
from lorebase_store.commit import (
    CommitError,
    CommitResult,
    SchemaChange,
    SchemaDelete,
    SchemaElementModel,
    SchemaUpsert,
    ValidationFailedError,
    commit,
)
from lorebase_store.commit.schema import schema_elements
from lorebase_store.materialize.rebuild import load_schema as load_tree_schema
from lorebase_store.nodestore import NodeRepository
from lorebase_store.repositories import MaterializedRepository, RevisionRow
from lorebase_store.runtime import StoreRuntime

SCHEMA_CHANGE_ORIGIN = "schema_change"
DEFAULT_MESSAGE = "Schema change"
DOCUMENT_ISSUE = "schema.document_invalid"
SECTIONS = ("kinds", "relations", "traits", "operations", "derivations", "sources")


class ChangeStatus(StrEnum):
    COMMITTED = "committed"
    DRY_RUN = "dry_run"
    NO_OP = "no_op"


@dataclass(frozen=True)
class ElementRemoval:
    """An element to remove, named by section and api name (or id)."""

    section: str
    name: str


@dataclass(frozen=True)
class ChangeSetInput:
    """A full document (`yaml`) or a patch (`patch` plus `remove`)."""

    yaml: str | None = None
    patch: Mapping[str, Any] | None = None
    remove: Sequence[ElementRemoval] = ()


@dataclass(frozen=True)
class ElementChange:
    """One element written or removed by a change set."""

    op: str
    section: str
    id: str
    api_name: str


@dataclass
class ChangeSetOutcome:
    """Result of applying or dry-running a change set."""

    status: ChangeStatus
    head: bytes
    revision: bytes | None
    changes: list[ElementChange]
    data_changes: int = 0
    issues: list[Finding] = field(default_factory=list)


class ChangeSetInvalidError(Exception):
    """The change set cannot be applied; `issues` locate the problems in the document."""

    def __init__(self, issues: Sequence[Finding]) -> None:
        super().__init__(f"{len(issues)} issue(s)")
        self.issues = list(issues)


class RemovalBlockedError(Exception):
    """Removing elements would delete data and `cascade` was not requested."""

    def __init__(self, issues: Sequence[Finding]) -> None:
        super().__init__(f"{len(issues)} element(s) hold data")
        self.issues = list(issues)


# Pure parts ---------------------------------------------------------------------------------


def _section_of(element: SchemaElementModel) -> str:
    return {
        "schema.domain": "domain",
        "schema.kind": "kinds",
        "schema.relation": "relations",
        "schema.trait": "traits",
        "schema.operation": "operations",
        "schema.derivation": "derivations",
        "schema.source": "sources",
    }[element.object_type]


def _api_name(element: SchemaElementModel) -> str:
    return str(getattr(element, "api_name", None) or element.id)


def diff_schemas(current: Schema, target: Schema) -> tuple[list[SchemaChange], list[ElementChange]]:
    """Schema changes turning `current` into `target`, with their description."""
    before = schema_elements(current)
    after = schema_elements(target)
    changes: list[SchemaChange] = []
    described: list[ElementChange] = []
    for key, element in after.items():
        old = before.get(key)
        if old is None or old.content() != element.content():
            changes.append(SchemaUpsert(element))
            op = "create" if old is None else "update"
            described.append(ElementChange(op, _section_of(element), key, _api_name(element)))
    for key, element in before.items():
        if key not in after:
            changes.append(SchemaDelete(key))
            described.append(ElementChange("delete", _section_of(element), key, _api_name(element)))
    return changes, described


def _document_findings(error: SchemaYamlError) -> list[Finding]:
    return [Finding(DOCUMENT_ISSUE, p.message, tuple(p.path)) for p in error.problems]


def _patched_document(current: Schema, change: ChangeSetInput) -> dict[str, Any]:
    document = schema_to_document(current)
    for section, items in (change.patch or {}).items():
        if section == "domain" and isinstance(items, Mapping):
            document["domain"] = {**document.get("domain", {}), **items}
        elif section in SECTIONS and isinstance(items, Mapping):
            document[section] = {**document.get(section, {}), **items}
        else:
            document[section] = items  # unknown sections are reported by the loader
    for removal in change.remove:
        elements = document.get(removal.section)
        if not isinstance(elements, dict):
            raise ChangeSetInvalidError([_issue((removal.section,), "unknown section")])
        name = removal.name
        if name not in elements:
            name = next(
                (
                    n
                    for n, body in elements.items()
                    if isinstance(body, Mapping) and body.get("id") == removal.name
                ),
                name,
            )
        if name not in elements:
            raise ChangeSetInvalidError(
                [_issue((removal.section, removal.name), "no such element to remove")]
            )
        del elements[name]
    return document


def _issue(path: tuple[str | int, ...], message: str) -> Finding:
    return Finding(DOCUMENT_ISSUE, message, path)


def target_schema(current: Schema, change: ChangeSetInput) -> Schema:
    """The schema a change set asks for; raises `ChangeSetInvalidError` when it cannot be read."""
    try:
        if change.yaml is not None:
            target = load_schema(change.yaml, base=current)
        else:
            target = schema_from_document(_patched_document(current, change), base=current)
    except SchemaYamlError as error:
        raise ChangeSetInvalidError(_document_findings(error)) from None
    if target.domain.id != current.domain.id:
        raise ChangeSetInvalidError([_issue(("domain", "id"), f"must be {current.domain.id!r}")])
    return target


def static_issues(target: Schema) -> list[Finding]:
    """Errors of static validation, located in the document (api names, YAML paths)."""
    return located(target, [i for i in validate_schema(target) if i.is_error])


def located(target: Schema, issues: Sequence[Issue]) -> list[Finding]:
    """Findings of core issues with ids replaced by api names."""
    return findings_of(locate_issues(target, issues))


# Store-backed parts -------------------------------------------------------------------------


async def schema_at(
    session: AsyncSession, domain_id: str, revision: RevisionRow, runtime: StoreRuntime
) -> Schema:
    """The schema stored in the tree of a Revision."""
    settings = runtime.settings
    nodes = NodeRepository(
        session, domain_id, writes=settings.writes, batch=settings.nodes.prefetch_batch
    )
    tree = await nodes.load_tree(revision.root)
    schema, _ = await load_tree_schema(session, domain_id, tree, runtime=runtime)
    return schema


async def read(store: StoreAccess, domain_id: str, target: ReadTarget) -> Schema:
    """The schema at a Revision the caller was authorized to read (`reads.resolve_readable`)."""
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        revision = await require_revision(session, domain_id, target.revision)
        return await schema_at(session, domain_id, revision, store.runtime)


async def validate(
    store: StoreAccess,
    domain_id: str,
    change: ChangeSetInput,
    revision: ReadTarget,
) -> tuple[list[Finding], list[ElementChange]]:
    """Static issues of a change set against the schema at an authorized Revision, and the
    element changes it would make. Nothing is written."""
    current = await read(store, domain_id, revision)
    try:
        target = target_schema(current, change)
    except ChangeSetInvalidError as invalid:
        return invalid.issues, []
    _, described = diff_schemas(current, target)
    return static_issues(target), described


async def apply(
    store: StoreAccess,
    domain_id: str,
    branch: str,
    change: ChangeSetInput,
    principal: Principal,
    *,
    message: str | None,
    expected_head: bytes | None,
    dry_run: bool,
    cascade: bool,
) -> ChangeSetOutcome:
    """Apply a change set to a branch as one Revision, or run it without committing.

    Raises `ChangeSetInvalidError` (document, static or data issues), `RemovalBlockedError`,
    `ProtectedBranchError` for direct writes to a protected branch, and the other
    `CommitError`s of the pipeline. A dry run reports data issues in the outcome instead.
    """
    async with store.sessions() as session:
        await session.begin()
        try:
            await require_domain(session, domain_id)
            ref = await require_branch(session, domain_id, branch)
            if ref.protected and not dry_run:
                raise ProtectedBranchError(branch)
            head_revision = await require_revision(session, domain_id, ref.head)
            current = await schema_at(session, domain_id, head_revision, store.runtime)
            target = target_schema(current, change)
            issues = static_issues(target)
            if issues:
                raise ChangeSetInvalidError(issues)
            changes, described = diff_schemas(current, target)
            if not cascade:
                await _check_removals(session, domain_id, branch, current, changes, store)
            outcome = ChangeSetOutcome(
                status=ChangeStatus.DRY_RUN if dry_run else ChangeStatus.COMMITTED,
                head=ref.head,
                revision=None,
                changes=described,
            )
            try:
                result = await commit(
                    session,
                    domain_id,
                    branch,
                    [],
                    schema_changes=changes,
                    author=author_of(principal),
                    message=message or DEFAULT_MESSAGE,
                    origin=Origin(type=SCHEMA_CHANGE_ORIGIN),
                    expected_head=expected_head,
                    runtime=store.runtime,
                    clock=store.clock,
                )
            except ValidationFailedError as error:
                if not dry_run:
                    raise ChangeSetInvalidError(located(target, error.issues)) from error
                outcome.issues = located(target, error.issues)
                return outcome
            _fill(outcome, result)
            if dry_run:
                outcome.revision = None
                return outcome
            await session.commit()
            return outcome
        finally:
            if session.in_transaction():
                await session.rollback()


def _fill(outcome: ChangeSetOutcome, result: CommitResult) -> None:
    if result.no_op:
        outcome.status = ChangeStatus.NO_OP
        outcome.revision = result.revision_id
        return
    outcome.revision = result.revision_id
    outcome.data_changes = sum(1 for e in result.changeset if e.partition != "schema")


async def _check_removals(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    current: Schema,
    changes: Sequence[SchemaChange],
    store: StoreAccess,
) -> None:
    removed = {c.element_id for c in changes if isinstance(c, SchemaDelete)}
    if not removed:
        return
    rows = MaterializedRepository(session, domain_id, branch, writes=store.runtime.settings.writes)
    records = await rows.record_counts()
    links = await rows.link_counts()
    blocked: list[Finding] = []
    for element_id in sorted(removed):
        kind = current.kinds.get(element_id)
        relation = current.relations.get(element_id)
        count = records.get(element_id, 0) if kind else links.get(element_id, 0)
        if (kind or relation) and count:
            name = kind.api_name if kind else relation.api_name  # type: ignore[union-attr]
            section = "kinds" if kind else "relations"
            what = "Records" if kind else "Links"
            blocked.append(
                _issue(
                    (section, name),
                    f"{name!r} holds {count} {what}; removing it deletes them. Pass cascade: "
                    "true to confirm.",
                )
            )
    if blocked:
        raise RemovalBlockedError(blocked)


__all__ = [
    "ChangeSetInput",
    "ChangeSetInvalidError",
    "ChangeSetOutcome",
    "ChangeStatus",
    "CommitError",
    "ElementChange",
    "ElementRemoval",
    "RemovalBlockedError",
    "apply",
    "diff_schemas",
    "read",
    "schema_at",
    "target_schema",
    "validate",
]
