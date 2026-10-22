"""Preview of a candidate Derivation on sample Records (design 5.8), without writing.

The candidate is a schema change set (`services.schema.ChangeSetInput`) naming the Derivation
to evaluate: the schema it produces is validated with the rules of schema changes
(`schema.expression_limits` included), then the Derivation is evaluated on a few Records of its
Kind at the branch head, from the stored and derived values of the branch, with the evaluator
the materializer uses (`lorebase_store.materialize.derived`). Nothing is stored.

What the caller may see follows Record reads: the Kinds the Derivation touches (its own, the
far Kind of an aggregate, the Kinds reached through `links.<relation>`) must be readable, and
the value is masked when the computed Attribute is sensitive in the schema read, in the default
branch's or in the candidate, which carries the tags of everything the Derivation may read
(`lorebase_core.expr.sensitivity`). A masked value hides its error message too.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.expr import (
    CompiledDerivation,
    ExprLimits,
    compile_derivation,
    derivation_reads,
    links_read,
)
from lorebase_core.model import (
    AggregateDerivation,
    Attribute,
    Derivation,
    ExpressionDerivation,
    Kind,
    Schema,
)
from lorebase_core.validate import SchemaRules
from lorebase_keeper.auth.read_audit import note_sensitive
from lorebase_keeper.reading.points import ReadPoint
from lorebase_keeper.reading.runtime import ReadRuntime
from lorebase_keeper.services.errors import Finding
from lorebase_keeper.services.schema import (
    ChangeSetInput,
    ChangeSetInvalidError,
    static_issues,
    target_schema,
)
from lorebase_store.materialize.derived import DerivedValues
from lorebase_store.repositories import MaterializedRepository

DERIVATION_NOT_FOUND = "derivation.not_found"
DERIVATION_UNSUPPORTED = "derivation.unsupported"


class CandidateInvalidError(Exception):
    """The candidate cannot be evaluated; `issues` locate why."""

    def __init__(self, issues: Sequence[Finding]) -> None:
        super().__init__(f"{len(issues)} issue(s)")
        self.issues = list(issues)


@dataclass(frozen=True)
class Candidate:
    """A validated candidate: the schema the change set asks for and the Derivation in it."""

    schema: Schema
    derivation: Derivation
    kind: Kind
    attribute: Attribute
    compiled: CompiledDerivation

    @property
    def touched(self) -> frozenset[str]:
        """Ids of the Kinds the Derivation reads Records of, its own included."""
        return touched_kinds(self.schema, self.derivation, self.compiled)


@dataclass(frozen=True)
class PreviewItem:
    """One sample Record: its key and title, and the value or the error masked for the caller.

    A masked item carries the masked value (none with `keeper.reads.sensitivity.omit`) and the
    error code only.
    """

    key: str
    title: str | None
    value: JsonValue
    masked: bool
    error_code: str | None = None
    error_message: str | None = None


def _issue(code: str, message: str, path: tuple[str | int, ...]) -> Finding:
    return Finding(code, message, path)


def candidate_of(
    current: Schema, change: ChangeSetInput, name: str, rules: SchemaRules
) -> Candidate:
    """The Derivation `name` of the schema `change` asks for, once the schema is valid.

    Raises `CandidateInvalidError` when the change set cannot be read, fails static validation
    (the issues of a schema change) or `name` is not an `expression` or `aggregate` Derivation
    of it. Path Derivations compose Links and compute no values.
    """
    try:
        schema = target_schema(current, change)
    except ChangeSetInvalidError as invalid:
        raise CandidateInvalidError(invalid.issues) from None
    issues = static_issues(schema, rules)
    if issues:
        raise CandidateInvalidError(issues)
    derivation = schema.derivation_by_api_name(name)
    if derivation is None:
        raise CandidateInvalidError(
            [
                _issue(
                    DERIVATION_NOT_FOUND, f"No Derivation {name!r} in the schema.", ("derivations",)
                )
            ]
        )
    definition = derivation.definition
    if not isinstance(definition, ExpressionDerivation | AggregateDerivation):
        raise CandidateInvalidError(
            [
                _issue(
                    DERIVATION_UNSUPPORTED,
                    "Only expression and aggregate Derivations compute values; a path "
                    "Derivation composes Links.",
                    ("derivations", name, "definition", "variant"),
                )
            ]
        )
    kind = schema.kinds[definition.kind]
    attribute = kind.attributes[definition.attribute]
    compiled = compile_derivation(schema, derivation, rules.expression_limits or ExprLimits())
    return Candidate(schema, derivation, kind, attribute, compiled)


def touched_kinds(
    schema: Schema, derivation: Derivation, compiled: CompiledDerivation
) -> frozenset[str]:
    """Ids of the Kinds whose Records the Derivation reads: its own, the Kinds of the
    Attributes it may read and the far Kinds of the Relations it follows."""
    definition = derivation.definition
    if not isinstance(definition, ExpressionDerivation | AggregateDerivation):
        return frozenset()
    kinds = {definition.kind}
    kinds.update(kind_id for kind_id, _ in derivation_reads(schema, derivation))
    if isinstance(definition, AggregateDerivation):
        relation = schema.relations.get(definition.relation)
        if relation is not None:
            kinds.add(relation.far_endpoint(definition.direction))
    for program in (compiled.expression, compiled.filter):
        if program is not None:
            kinds.update(f.relation.far_endpoint(f.direction) for f in links_read(program))
    return frozenset(kinds)


def kind_names(schema: Schema, kind_ids: frozenset[str]) -> list[str]:
    """Api names of Kinds, sorted."""
    return sorted(schema.kinds[k].api_name for k in kind_ids if k in schema.kinds)


async def preview(
    session: AsyncSession,
    runtime: ReadRuntime,
    point: ReadPoint,
    candidate: Candidate,
    *,
    keys: Sequence[str] | None,
    limit: int,
) -> list[PreviewItem]:
    """Evaluate the candidate on `keys` (the first `limit` Records of its Kind by key when
    `None`), at the branch head `point` names, in the read transaction of `session`.

    The caller has checked the Kinds the candidate touches against the point's scope.
    """
    store = runtime.store
    repository = MaterializedRepository(
        session,
        point.domain.id,
        point.branch,
        writes=store.settings.writes,
        layout=point.layout,
    )
    materialize = store.settings.materialize
    values = DerivedValues(
        repository,
        candidate.schema,
        limits=store.schema.expression_limits,
        now=point.revision.created_at,
        batch=materialize.read_batch_rows,
        max_incremental=materialize.derivation_max_incremental_records,
    )
    masking = point.masking.over(point.schema, candidate.schema)
    masked = masking.hidden(candidate.attribute, (candidate.kind.id,))
    items: list[PreviewItem] = []
    for found in await values.preview(candidate.attribute.id, keys=keys, limit=limit):
        title = masking.title(candidate.schema, candidate.kind.id, found.title)
        if masked:
            note_sensitive("masked")
            items.append(
                PreviewItem(
                    found.key,
                    title,
                    None if masking.settings.omit else masking.masked,
                    True,
                    None if found.error is None else found.error.code.value,
                )
            )
            continue
        if masking.sensitive(candidate.attribute):
            note_sensitive("shown")
        error = found.error
        items.append(
            PreviewItem(
                found.key,
                title,
                found.value,
                False,
                None if error is None else error.code.value,
                None if error is None else error.describe(),
            )
        )
    return items
