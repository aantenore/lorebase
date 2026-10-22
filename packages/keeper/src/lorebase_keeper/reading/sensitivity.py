"""Masking of sensitive values and disclosure of vault values in reads (design 10.2, 13.5).

**Clearance.** Values of an Attribute tagged `pii`, `confidential` or `restricted` are shown
only to callers holding the matching permission (`pii.read`, `confidential.read`,
`restricted.read`) through a Domain-wide grant or a grant narrowed to the Kind holding the
value. Grants narrowed to a branch do not count: whoever may create branches could name one to
match. Kind-narrowed grants name Kinds by api name; they are resolved to Kind ids in the
schema of the Domain's default branch (the reference schema), so renaming a Kind on another
branch does not move a clearance. An Attribute with several tags needs every clearance; Link
Attributes need it on both Kinds of the Relation. Callers without it get
`keeper.reads.sensitivity.masked_value` (or nothing, with `omit`).

**Tags.** An Attribute's tags are those of the schema read plus those the same Attribute (by
id) has in the reference schema, carried through Derivations (`Masking.over`), so reading an
older Revision, or a branch forked before a tag was added, never shows less than the default
branch would. Removing a tag (or a sensitive Attribute, Kind or Relation) on every branch needs
the clearance for it (`require_lowering`; the commit pipeline and the merge service refuse it
otherwise).

**Two layers.**

- `Masking` runs where values are rendered (Records, Links, history, lineage, diffs,
  Conflicts, Operation edits, explain and context packs): it masks by schema, before a body is
  cached. Response caches are keyed by `Clearance.variant`, so callers with different
  clearances never share a body.
- `Disclosure` runs on the finished body, after the cache: every vault reference
  (`{"$vault": token}`) still in it is replaced by its value when the vault row's Kind and
  Attribute are cleared for the caller, by `erased_value` when it was erased, and by
  `masked_value` otherwise (unknown tokens and keys that are not configured included). A read
  path that forgets to mask still never discloses a vault value, and cached bodies hold
  tokens, not personal data, so an erasure takes effect on the next read.

Filtering, ordering and grouping on a sensitive Attribute need the clearance (403
otherwise); on a vault Attribute they are refused (422): its stored values are tokens.

**Validation issues.** The code of an issue about a value (`value.pattern`, `value.enum_values`,
...) tells what the value looks like. Issues about values of an Attribute the caller may not
read are reported with the generic `value.invalid` and a message without the rule
(`generalize_issues`); path, Kind, key and Attribute stay.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from threading import Lock
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.expr.sensitivity import effective_tags, lowered_tags, schema_tags
from lorebase_core.model import (
    VAULT_KEY,
    Attribute,
    Kind,
    Relation,
    Schema,
    Sensitivity,
    vault_token,
)
from lorebase_core.query import (
    Comparison,
    Condition,
    Conjunction,
    Disjunction,
    FieldRef,
    FieldSource,
    LinkCount,
    LinkExists,
    Negation,
    QueryPlan,
)
from lorebase_core.validate import Issue, IssueCode
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.auth.rbac import AccessContext, Authorizer
from lorebase_keeper.auth.read_audit import note_sensitive
from lorebase_keeper.errors import FORBIDDEN, ProblemError
from lorebase_keeper.reading.problems import invalid
from lorebase_keeper.settings import SensitivitySettings
from lorebase_store.materialize.rows import PartitionType, parse_partition
from lorebase_store.materialize.sensitivity import default_branch_schema
from lorebase_store.runtime import StoreRuntime
from lorebase_store.vault import VaultKeys, VaultState, open_values

CLEARANCES: Mapping[Sensitivity, Permission] = MappingProxyType(
    {
        Sensitivity.PII: Permission.PII_READ,
        Sensitivity.CONFIDENTIAL: Permission.CONFIDENTIAL_READ,
        Sensitivity.RESTRICTED: Permission.RESTRICTED_READ,
    }
)
"""The permission that clears each sensitivity tag."""

_REFERENCE_MARK = json.dumps(VAULT_KEY).encode()
_TEXT_REFERENCE = re.compile(r'\{\s*"\$vault"\s*:\s*"(vlt_[0-9a-f]{32})"\s*\}')


@dataclass(frozen=True, slots=True)
class Clearance:
    """Sensitivity tags a caller may read: on every Kind (`everywhere`), or on some Kinds by
    id (`kinds`, from grants narrowed to a Kind)."""

    everywhere: frozenset[Sensitivity] = frozenset()
    kinds: Mapping[Sensitivity, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def full(cls) -> Clearance:
        return cls(everywhere=frozenset(Sensitivity))

    def covers(self, tags: Iterable[Sensitivity], kinds: Sequence[str]) -> bool:
        """Whether every tag is cleared on every Kind in `kinds`."""
        for tag in tags:
            if tag in self.everywhere:
                continue
            allowed = self.kinds.get(tag, frozenset())
            if not kinds or any(kind not in allowed for kind in kinds):
                return False
        return True

    @property
    def variant(self) -> str:
        """Distinguishes cached responses built for callers with different clearances."""
        parts = [f"{tag.value}:*" for tag in sorted(self.everywhere)]
        parts.extend(
            f"{tag.value}:{','.join(sorted(kinds))}"
            for tag, kinds in sorted(self.kinds.items())
            if kinds and tag not in self.everywhere
        )
        return "clear=" + ";".join(parts) if parts else ""


async def clearance_of(
    authorizer: Authorizer, principal: Principal, domain_id: str, reference: Schema | None
) -> Clearance:
    """The caller's clearance in a Domain: tags cleared by Domain-wide grants, and by grants
    narrowed to a Kind, resolved to Kind ids in `reference` (the default branch's schema;
    names it does not know clear nothing)."""
    everywhere: set[Sensitivity] = set()
    kinds: dict[Sensitivity, frozenset[str]] = {}
    for tag, permission in CLEARANCES.items():
        if await authorizer.allows(principal, permission, domain_id, AccessContext()):
            everywhere.add(tag)
            continue
        granted = await authorizer.granted_kinds(principal, permission, domain_id)
        ids = frozenset(
            kind.id
            for name in granted
            if reference is not None and (kind := reference.kind_by_api_name(name)) is not None
        )
        if ids:
            kinds[tag] = ids
    return Clearance(frozenset(everywhere), kinds)


async def default_schema(
    session: AsyncSession, domain_id: str, runtime: StoreRuntime
) -> Schema | None:
    """The schema at the head of the Domain's default branch (the reference schema), from the
    store's schema cache; None when the Domain or its materialized head is missing."""
    return await default_branch_schema(session, domain_id, runtime=runtime)


def reference_tags(schema: Schema | None) -> dict[str, frozenset[Sensitivity]]:
    """Attribute id -> sensitivity tags, for the Attributes of `schema` that have any."""
    return schema_tags(schema)


async def masking_for(
    authorizer: Authorizer,
    principal: Principal,
    domain_id: str,
    reference: Schema | None,
    settings: SensitivitySettings,
) -> Masking:
    """The caller's masking in a Domain whose default branch has the schema `reference`."""
    clearance = await clearance_of(authorizer, principal, domain_id, reference)
    return Masking(clearance, settings, MappingProxyType(reference_tags(reference)))


async def require_lowering(
    authorizer: Authorizer,
    principal: Principal,
    domain_id: str,
    old: Schema,
    new: Schema,
) -> None:
    """403 when `new` drops sensitivity tags of `old` the caller is not cleared for
    Domain-wide, on any branch."""
    lowered = lowered_tags(old, new)
    if not lowered:
        return
    clearance = await clearance_of(authorizer, principal, domain_id, None)
    missing = sorted(CLEARANCES[tag].value for tag in lowered - clearance.everywhere)
    if missing:
        raise ProblemError(
            FORBIDDEN,
            "Removing sensitivity tags (or sensitive Attributes) needs "
            f"{' and '.join(missing)} on the Domain.",
            extensions={"code": "sensitivity_lowered", "permission": missing[0]},
        )


@dataclass(frozen=True, slots=True)
class Masking:
    """Masks values the caller is not cleared for, where they are rendered."""

    clearance: Clearance
    settings: SensitivitySettings = field(default_factory=SensitivitySettings)
    reference: Mapping[str, frozenset[Sensitivity]] = field(default_factory=dict)
    """Tags of the default branch's Attributes by id (`reference_tags`)."""
    effective: Mapping[str, frozenset[Sensitivity]] = field(default_factory=dict)
    """Tags of the schemas read, with the reference's, carried through Derivations (`over`)."""

    def over(self, *schemas: Schema) -> Masking:
        """This masking for reads of `schemas`: a derived Attribute carries the tags of what it
        reads, as the default branch and the schemas read tag them (`effective_tags`)."""
        effective: dict[str, frozenset[Sensitivity]] = {}
        for schema in schemas:
            for key, tags in _effective(schema, self.reference).items():
                effective[key] = effective.get(key, frozenset()) | tags
        return replace(self, effective=MappingProxyType(effective))

    @property
    def variant(self) -> str:
        """What a cached response depends on: the clearance and the default branch's tags
        (a tag added there changes what older Revisions show)."""
        if not self.reference:
            return self.clearance.variant
        digest = hashlib.sha256(
            json.dumps(
                sorted((k, sorted(t.value for t in v)) for k, v in self.reference.items())
            ).encode()
        ).hexdigest()[:16]
        return "|".join(p for p in (self.clearance.variant, f"tags={digest}") if p)

    def tags(self, attribute: Attribute) -> frozenset[Sensitivity]:
        """The Attribute's tags in the schema read, in the reference schema and through what
        it is derived from."""
        return (
            frozenset(attribute.sensitivity)
            | self.reference.get(attribute.id, frozenset())
            | self.effective.get(attribute.id, frozenset())
        )

    def sensitive(self, attribute: Attribute) -> bool:
        """Whether the Attribute is sensitive by any of its tags (cleared or not)."""
        return bool(self.tags(attribute))

    def title(self, schema: Schema, kind_id: str, title: str | None) -> str | None:
        """A stored Record title, or None when the Kind's title Attribute is sensitive: rows
        materialized before a tag was added (on the default branch, after this branch forked)
        may still hold one."""
        kind = schema.kinds.get(kind_id)
        if title is None or kind is None or kind.title_attribute is None:
            return title
        attribute = kind.attributes.get(kind.title_attribute)
        return None if attribute is not None and self.sensitive(attribute) else title

    def hidden(self, attribute: Attribute, kinds: Sequence[str]) -> bool:
        """Whether values of `attribute` (held by Records or Links of the Kinds `kinds`, by
        id) are masked."""
        tags = self.tags(attribute)
        return bool(tags) and not self.clearance.covers(tags, kinds)

    @property
    def masked(self) -> str:
        return self.settings.masked_value

    def values(
        self,
        attributes: Mapping[str, Attribute],
        values: Mapping[str, Any],
        kinds: Sequence[str],
        derived: Mapping[str, Any] | None = None,
        *,
        omit: bool | None = None,
    ) -> dict[str, Any]:
        """Values keyed by api name in schema order, masked; ids unknown to `attributes` are
        dropped. `omit` defaults to the setting."""
        leave_out = self.settings.omit if omit is None else omit
        out: dict[str, Any] = {}
        for attribute_id, attribute in attributes.items():
            source = derived if attribute.is_derived and derived is not None else values
            if attribute_id not in source:
                continue
            if self.hidden(attribute, kinds):
                note_sensitive("masked")
                if not leave_out:
                    out[attribute.api_name] = self.masked
                continue
            if self.sensitive(attribute):
                note_sensitive("shown")
            out[attribute.api_name] = source[attribute_id]
        return out

    def value(self, attribute: Attribute | None, kinds: Sequence[str], value: Any) -> Any:
        """One value outside a value map (history, lineage, diffs): masked, never omitted.
        An Attribute the schema no longer knows is masked unless the caller is cleared for
        every tag."""
        if value is None:
            return None
        if attribute is None:
            return value if self.clearance.covers(Sensitivity, kinds) else self.masked
        if self.hidden(attribute, kinds):
            note_sensitive("masked")
            return self.masked
        if self.sensitive(attribute):
            note_sensitive("shown")
        return value

    def kind_values(self, kind: Kind, values: Mapping[str, Any], **options: Any) -> dict[str, Any]:
        return self.values(kind.attributes, values, (kind.id,), **options)

    def link_values(
        self, schema: Schema, relation: Relation, values: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        return self.values(relation.attributes, values or {}, relation_kinds(schema, relation))

    def value_map(
        self, attributes: Mapping[str, Attribute], kinds: Sequence[str], values: Any
    ) -> Any:
        """A value map keyed by Attribute id (or an element body holding `values`), masked;
        ids unknown to `attributes` are masked unless the caller is cleared for everything."""
        if not isinstance(values, dict):
            return values
        if isinstance(values.get("values"), dict):
            return {**values, "values": self.value_map(attributes, kinds, values["values"])}
        return {key: self.value(attributes.get(key), kinds, item) for key, item in values.items()}

    def require_readable(self, attribute: Attribute, kinds: Sequence[str], pointer: str) -> None:
        """403 when a query filters, orders or groups by a value the caller may not read."""
        if not self.hidden(attribute, kinds):
            return
        tags = sorted(t.value for t in self.tags(attribute))
        missing = [CLEARANCES[Sensitivity(t)].value for t in tags]
        raise ProblemError(
            FORBIDDEN,
            f"{attribute.api_name} is sensitive ({', '.join(tags)}): filtering, ordering or "
            f"grouping by it needs {' and '.join(missing)}.",
            extensions={
                "code": "sensitive_attribute",
                "attribute": attribute.api_name,
                "pointer": pointer,
                "permission": missing[0],
            },
        )


_EffectiveKey = tuple[int, frozenset[tuple[str, frozenset[Sensitivity]]]]
_EFFECTIVE: OrderedDict[_EffectiveKey, tuple[Schema, dict[str, frozenset[Sensitivity]]]] = (
    OrderedDict()
)
_EFFECTIVE_ENTRIES = 256
_EFFECTIVE_LOCK = Lock()


def _effective(
    schema: Schema, reference: Mapping[str, frozenset[Sensitivity]]
) -> dict[str, frozenset[Sensitivity]]:
    """`effective_tags`, remembered per schema object and reference tags (compiled schemas
    are cached, so the same object comes back for the same schema)."""
    key = (id(schema), frozenset(reference.items()))
    with _EFFECTIVE_LOCK:
        found = _EFFECTIVE.get(key)
        if found is not None and found[0] is schema:
            _EFFECTIVE.move_to_end(key)
            return found[1]
    computed = effective_tags(schema, reference)
    with _EFFECTIVE_LOCK:
        _EFFECTIVE[key] = (schema, computed)
        while len(_EFFECTIVE) > _EFFECTIVE_ENTRIES:
            _EFFECTIVE.popitem(last=False)
    return computed


def relation_kinds(_schema: Schema, relation: Relation) -> tuple[str, ...]:
    """Ids of both Kinds of a Relation."""
    return (relation.from_kind, relation.to_kind)


UNCLEARED = Masking(Clearance())
"""Masking for a caller without any clearance."""


class Disclosure:
    """Replaces vault references in a finished response body (see module doc)."""

    def __init__(
        self,
        session: AsyncSession,
        domain_id: str,
        schemas: Sequence[Schema],
        masking: Masking,
        keys: VaultKeys | None,
    ) -> None:
        self._session = session
        self._domain_id = domain_id
        self._schemas = tuple(schemas)
        self._masking = masking
        self._keys = keys

    async def send(self, body: bytes, media_type: str) -> bytes:
        """A body about to be sent, with its vault references resolved: JSON documents are
        parsed, other text (markdown) has the JSON form of each reference replaced."""
        if _REFERENCE_MARK not in body:
            return body
        kind = media_type.split(";")[0].strip()
        if kind == "application/json" or kind.endswith("+json"):
            return await self.body(body)
        if kind in ("application/x-ndjson", "application/jsonl"):
            lines = [await self.body(line) if line else line for line in body.split(b"\n")]
            return b"\n".join(lines)
        return await self.text(body)

    async def text(self, body: bytes) -> bytes:
        """Text with every JSON-rendered reference replaced by what the caller may see."""
        content = body.decode()
        tokens = {m.group(1) for m in _TEXT_REFERENCE.finditer(content)}
        if not tokens:
            return body
        shown = await self._shown(tokens)

        def render(match: re.Match[str]) -> str:
            value = shown[match.group(1)]
            plain = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            return markdown_text(plain)

        return _TEXT_REFERENCE.sub(render, content).encode()

    async def body(self, body: bytes) -> bytes:
        """A JSON body with its vault references resolved; unchanged when it holds none."""
        if _REFERENCE_MARK not in body:
            return body
        data = json.loads(body)
        if not await self.resolve(data):
            return body
        return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()

    async def model(self, model: BaseModel) -> bytes:
        """The JSON body of `model`, with its vault references resolved."""
        return await self.body(model.model_dump_json().encode())

    async def resolve(self, data: JsonValue) -> bool:
        """Resolve the references inside `data` in place; whether there were any."""
        slots = list(_references(data))
        if not slots:
            return False
        shown = await self._shown({token for _, _, token in slots})
        for container, slot, token in slots:
            container[slot] = shown[token]
        return True

    async def _shown(self, tokens: set[str]) -> dict[str, JsonValue]:
        """What the caller sees for each token."""
        opened = await open_values(self._session, self._domain_id, tokens, self._keys)
        settings = self._masking.settings
        out: dict[str, JsonValue] = {}
        for token, found in opened.items():
            if not self._cleared(found.kind_id, found.attribute_id):
                note_sensitive("masked")
                out[token] = settings.masked_value
            elif found.state is VaultState.VALUE:
                note_sensitive("shown")
                out[token] = found.value
            elif found.state is VaultState.ERASED:
                out[token] = settings.erased_value
            else:
                out[token] = settings.masked_value
        return out

    def _cleared(self, kind_id: str | None, attribute_id: str | None) -> bool:
        """Whether the caller may read a value of the vault row's Kind and Attribute: `pii`,
        the Attribute's tags in the schemas read and in the reference schema."""
        if kind_id is None or attribute_id is None:
            return False  # unknown token: nothing to disclose
        tags: set[Sensitivity] = {Sensitivity.PII}
        tags |= self._masking.reference.get(attribute_id, frozenset())
        for schema in self._schemas:
            kind = schema.kinds.get(kind_id)
            attribute = None if kind is None else kind.attributes.get(attribute_id)
            if attribute is not None:
                tags |= set(attribute.sensitivity)
        return self._masking.clearance.covers(tags, (kind_id,))


_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]<>()#+!|~])")


def markdown_text(text: str) -> str:
    """`text` safe inside markdown and table cells: one line, markup characters escaped."""
    return _MARKDOWN_SPECIAL.sub(r"\\\1", " ".join(text.split()))


def _references(data: Any) -> Iterator[tuple[Any, Any, str]]:
    """`(container, key or index, token)` of every vault reference inside `data`."""
    stack: list[Any] = [data]
    while stack:
        current = stack.pop()
        items: Iterable[tuple[Any, Any]]
        if isinstance(current, dict):
            items = current.items()
        elif isinstance(current, list):
            items = enumerate(current)
        else:
            continue
        for slot, item in items:
            token = vault_token(item)
            if token is not None:
                yield current, slot, token
            elif isinstance(item, dict | list):
                stack.append(item)


FieldUse = tuple[FieldRef, tuple[Kind, ...]]
"""A field compared, and the Kinds whose Records it is read from."""


def condition_fields(condition: Condition | None, kinds: Sequence[Kind]) -> Iterator[FieldUse]:
    """Fields a condition over Records of `kinds` compares, through Relation predicates too
    (whose fields are read from the far Kinds)."""
    match condition:
        case Comparison(field=found):
            yield found, tuple(kinds)
        case LinkExists(hop=hop, where=where) | LinkCount(hop=hop, where=where):
            yield from condition_fields(where, hop.far.kinds)
        case Conjunction(items=items) | Disjunction(items=items):
            for item in items:
                yield from condition_fields(item, kinds)
        case Negation(item=item):
            yield from condition_fields(item, kinds)


def require_comparable(
    schema: Schema, masking: Masking, fields: Iterable[FieldUse], pointer: str
) -> None:
    """Refuse filtering, ordering or grouping by values the caller may not read (403) or that
    are stored in the vault (422: Records hold tokens, which do not compare like values). The
    `title` field is the title Attribute's value: it needs the same clearance."""
    for ref, kinds in fields:
        if ref.source is FieldSource.TITLE:
            for kind in kinds:
                title = kind.attributes.get(kind.title_attribute or "")
                if title is not None:
                    masking.require_readable(title, (kind.id,), pointer)
            continue
        if ref.source not in (FieldSource.VALUE, FieldSource.DERIVED):
            continue
        for kind_id, attribute_id in sorted(ref.attributes.items()):
            owner = schema.kinds.get(kind_id)
            attribute = None if owner is None else owner.attributes.get(attribute_id)
            if owner is None or attribute is None:
                continue
            if attribute.in_vault:
                raise invalid(
                    pointer,
                    f"{attribute.api_name} is stored in the vault and cannot be filtered, "
                    "ordered or grouped by",
                    "vault_attribute",
                )
            masking.require_readable(attribute, (owner.id,), pointer)


def require_comparable_plan(schema: Schema, masking: Masking, plan: QueryPlan, prefix: str) -> None:
    """`require_comparable` over everything a query plan compares."""
    kinds = plan.target.kinds
    require_comparable(schema, masking, condition_fields(plan.where, kinds), f"{prefix}/where")
    require_comparable(
        schema, masking, ((o.field, kinds) for o in plan.order), f"{prefix}/order_by"
    )
    if plan.aggregate is not None and plan.aggregate.group_by is not None:
        require_comparable(
            schema, masking, ((plan.aggregate.group_by, kinds),), f"{prefix}/aggregate/group_by"
        )
    for include in plan.include:
        require_comparable(
            schema,
            masking,
            condition_fields(include.where, include.hop.far.kinds),
            f"{prefix}/include",
        )


GENERALIZED_CODES: frozenset[IssueCode] = frozenset(
    {
        IssueCode.VALUE_TYPE,
        IssueCode.VALUE_NOT_CANONICAL,
        IssueCode.VALUE_MIN,
        IssueCode.VALUE_MAX,
        IssueCode.VALUE_MIN_LENGTH,
        IssueCode.VALUE_MAX_LENGTH,
        IssueCode.VALUE_PATTERN,
        IssueCode.VALUE_ENUM_VALUES,
        IssueCode.VALUE_UNIQUE,
        IssueCode.VALUE_REFERENCE_MISSING,
        IssueCode.VALUE_VAULT_REFERENCE,
        IssueCode.RECORD_TRAIT_CONFORMANCE,
    }
)
"""Issue codes that tell something about a value beyond its absence."""


def generalize_issues(
    issues: Iterable[Issue], schemas: Sequence[Schema], masking: Masking
) -> list[Issue]:
    """`issues` (with element ids, as the core reports them) where those about a value the
    caller may not read have the code `value.invalid`, a message naming only the Attribute,
    and no details. The Attribute is looked up in `schemas` in turn; one none of them knows is
    treated as sensitive unless the caller is cleared for every tag."""
    out: list[Issue] = []
    for issue in issues:
        if issue.code in GENERALIZED_CODES and issue.attribute is not None:
            name = _issue_hidden(issue, schemas, masking)
            if name is not None:
                issue = issue.model_copy(
                    update={
                        "code": IssueCode.VALUE_INVALID,
                        "message": f"{name!r} is not valid",
                        "details": {},
                    }
                )
        out.append(issue)
    return out


def _issue_hidden(issue: Issue, schemas: Sequence[Schema], masking: Masking) -> str | None:
    """The api name of the issue's Attribute when the caller may not read its values."""
    attribute_id = issue.attribute or ""
    for schema in schemas:
        attributes: Mapping[str, Attribute]
        if issue.relation is not None:
            relation = schema.relations.get(issue.relation)
            if relation is None:
                continue
            attributes, kinds = relation.attributes, relation_kinds(schema, relation)
        else:
            kind = schema.kinds.get(issue.kind or "")
            if kind is None:
                continue
            attributes, kinds = kind.attributes, (kind.id,)
        attribute = attributes.get(attribute_id)
        if attribute is None:
            continue
        tags = masking.tags(attribute) | ({Sensitivity.PII} if attribute.in_vault else set())
        hidden = bool(tags) and not masking.clearance.covers(tags, kinds)
        return attribute.api_name if hidden else None
    kinds = tuple(k for k in (issue.kind,) if k is not None)
    return None if masking.clearance.covers(Sensitivity, kinds) else attribute_id


_CONFLICT_VALUES = ("base", "ours", "theirs")


def _element_attributes(
    schema: Schema | None, partition: str
) -> tuple[Mapping[str, Attribute], tuple[str, ...]] | None:
    """Attributes and holder Kinds of a data partition; None for the schema partition."""
    try:
        parsed = parse_partition(partition)
    except ValueError:
        return None
    if parsed.type is PartitionType.SCHEMA:
        return None
    if schema is None:
        return {}, (parsed.element_id,) if parsed.type is PartitionType.KIND else ()
    if parsed.type is PartitionType.KIND:
        kind = schema.kinds.get(parsed.element_id)
        return ({}, (parsed.element_id,)) if kind is None else (kind.attributes, (kind.id,))
    relation = schema.relations.get(parsed.element_id)
    if relation is None:
        return {}, ()
    return relation.attributes, relation_kinds(schema, relation)


def conflict_hidden(payload: Mapping[str, Any], schema: Schema | None, masking: Masking) -> bool:
    """Whether a Conflict is about one Attribute whose values the caller may not read (an
    Attribute the schema does not know counts as sensitive)."""
    attribute_id = payload.get("attribute")
    located = _element_attributes(schema, str(payload.get("partition", "")))
    if located is None or not isinstance(attribute_id, str):
        return False
    attributes, kinds = located
    attribute = attributes.get(attribute_id)
    if attribute is None:
        return not masking.clearance.covers(Sensitivity, kinds)
    return masking.hidden(attribute, kinds)


def mask_conflict(
    payload: Mapping[str, Any], schema: Schema | None, masking: Masking
) -> dict[str, Any]:
    """A stored Conflict with the values the caller may not read masked: the three versions
    of a sensitive Attribute (with the message and details, which may quote them), or the
    sensitive entries of whole value maps."""
    out = dict(payload)
    located = _element_attributes(schema, str(payload.get("partition", "")))
    if located is None:
        return out
    if conflict_hidden(payload, schema, masking):
        for name in _CONFLICT_VALUES:
            if out.get(name) is not None:
                out[name] = masking.masked
        out["message"] = "The versions of a sensitive Attribute differ."
        out["details"] = {}
        return out
    if payload.get("attribute") is None:
        attributes, kinds = located
        for name in _CONFLICT_VALUES:
            out[name] = masking.value_map(attributes, kinds, out.get(name))
    return out
