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
(`lorebase_core.expr.sensitivity`), unless the caller is cleared for each tag on the
Derivation's Kind and on every Kind the tag comes from (`tag_origins`: a value computed from a
sensitive Attribute of a far Kind tells about its Records). A masked value only says whether
it failed: no error code, no message.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
from lorebase_core.expr.sensitivity import schema_tags, tag_origins
from lorebase_core.expr.visibility import element_ids
from lorebase_core.model import (
    AggregateDerivation,
    Attribute,
    Derivation,
    ExpressionDerivation,
    Kind,
    LinkDirection,
    Schema,
    Sensitivity,
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
DERIVATION_ID_UNAVAILABLE = "derivation.id_unavailable"


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
    limits: ExprLimits

    @property
    def touched(self) -> frozenset[str]:
        """Ids of the Kinds the Derivation reads Records of, its own included."""
        return touched_kinds(self.schema, self.derivation, self.compiled, self.limits)


@dataclass(frozen=True)
class PreviewItem:
    """One sample Record: its key and title, and the value or the error masked for the caller.

    A masked item carries the masked value (none with `keeper.reads.sensitivity.omit`) and
    whether the value failed (`failed`), without the error code or message.
    """

    key: str
    title: str | None
    value: JsonValue
    masked: bool
    failed: bool = False
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
    limits = rules.expression_limits or ExprLimits()
    compiled = compile_derivation(schema, derivation, limits)
    return Candidate(schema, derivation, kind, attribute, compiled, limits)


def require_new_ids(candidate: Candidate, hidden: frozenset[str]) -> None:
    """Refuse a candidate built on a projected schema (`ReadPoint.hidden`) that reuses an id
    the projection left out: Records and values stored under it are not the caller's to read
    (design 13.3)."""
    reused = sorted(element_ids(candidate.schema) & hidden)
    if reused:
        raise CandidateInvalidError(
            [_issue(DERIVATION_ID_UNAVAILABLE, f"Id {reused[0]} cannot be used here.", ())]
        )


def touched_kinds(
    schema: Schema,
    derivation: Derivation,
    compiled: CompiledDerivation,
    limits: ExprLimits | None = None,
) -> frozenset[str]:
    """Ids of the Kinds whose Records the Derivation reads: its own, the Kinds of the
    Attributes it may read and the far Kinds of the Relations it follows (every Kind a path
    Derivation crosses)."""
    definition = derivation.definition
    if not isinstance(definition, ExpressionDerivation | AggregateDerivation):
        return frozenset()
    paths = schema.path_relations()
    kinds = {definition.kind}
    kinds.update(kind_id for kind_id, _ in derivation_reads(schema, derivation, limits))
    followed: list[tuple[str, LinkDirection]] = []
    if isinstance(definition, AggregateDerivation):
        followed.append((definition.relation, definition.direction))
    for program in (compiled.expression, compiled.filter):
        if program is not None:
            followed.extend((f.relation.id, f.direction) for f in links_read(program))
    for relation_id, direction in followed:
        relation = schema.link_relation(relation_id)
        if relation is not None:
            kinds.add(relation.far_endpoint(direction))
        if relation_id in paths:
            kinds.update(paths[relation_id].kinds)
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
        fanout=store.settings.traverse.path_fanout,
    )
    masking = point.masking.over(point.schema, candidate.schema)
    origins = tag_origins(
        candidate.schema,
        candidate.attribute.id,
        _united(masking.reference, schema_tags(point.schema)),
        candidate.limits,
    )
    masked = masking.hidden_with_origins(candidate.attribute, (candidate.kind.id,), origins)
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
                    found.error is not None,
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
                error is not None,
                None if error is None else error.code.value,
                None if error is None else error.describe(),
            )
        )
    return items


def _united(*tag_maps: Mapping[str, frozenset[Sensitivity]]) -> dict[str, frozenset[Sensitivity]]:
    """Attribute id -> tags, united over `tag_maps`."""
    united: dict[str, frozenset[Sensitivity]] = {}
    for tags in tag_maps:
        for attribute_id, found in tags.items():
            united[attribute_id] = united.get(attribute_id, frozenset()) | found
    return united
