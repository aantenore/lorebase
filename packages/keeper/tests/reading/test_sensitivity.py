"""Clearances and masking of sensitive values (design 10.2): unit rules, no database."""

from __future__ import annotations

from typing import Any

import pytest

from lorebase_core.expr import ExprError, ExprErrorCode
from lorebase_core.model import (
    Attribute,
    Derivation,
    Domain,
    ExpressionDerivation,
    Kind,
    Record,
    Schema,
    Sensitivity,
)
from lorebase_core.query import Query, QueryLimits, resolve_query
from lorebase_core.tree import kind_partition
from lorebase_core.types import TypeSpec
from lorebase_core.validate import Issue, IssueCode, validate_record
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.errors import ProblemError
from lorebase_keeper.reading.sensitivity import (
    Clearance,
    Disclosure,
    Masking,
    clearance_of,
    generalize_issues,
    lowered_tags,
    mask_conflict,
    masking_for,
    require_comparable_plan,
    require_lowering,
)
from lorebase_keeper.routes.proposal_items import _scrub, _shown
from lorebase_keeper.services.operations import (
    _expression_finding,
    _redactor,
    recorded_parameters,
    resolve,
)
from lorebase_keeper.settings import SensitivitySettings

KIND = "knd_01JB8Z00000000000000000001"
CODE, NAME, PHONE, EMAIL, SECRET = (f"att_01JB8Z0000000000000000000{n}" for n in range(1, 6))
S = TypeSpec.of("string")


def _attribute(attribute_id: str, name: str, *tags: Sensitivity, **extra: Any) -> Attribute:
    return Attribute(id=attribute_id, api_name=name, type=S, sensitivity=tags, **extra)


SCHEMA = Schema(
    domain=Domain(id="people"),
    kinds={
        KIND: Kind(
            id=KIND,
            api_name="Person",
            primary_key=(CODE,),
            attributes={
                a.id: a
                for a in (
                    _attribute(CODE, "code", required=True),
                    _attribute(NAME, "name"),
                    _attribute(PHONE, "phone", Sensitivity.PII),
                    _attribute(EMAIL, "email", Sensitivity.PII, vault=True),
                    _attribute(SECRET, "secret", Sensitivity.PII, Sensitivity.RESTRICTED),
                )
            },
        )
    },
)
PERSON = SCHEMA.kinds[KIND]
VALUES = {CODE: "P-1", NAME: "Ada", PHONE: "555", SECRET: "x"}


def test_clearance_covers_tags_everywhere_or_on_kinds() -> None:
    nothing = Clearance()
    domain = Clearance(everywhere=frozenset({Sensitivity.PII}))
    person = Clearance(kinds={Sensitivity.PII: frozenset({KIND})})
    assert not nothing.covers([Sensitivity.PII], [KIND])
    assert domain.covers([Sensitivity.PII], [KIND])
    assert person.covers([Sensitivity.PII], [KIND])
    assert not person.covers([Sensitivity.PII], [KIND, "knd_other"])  # Link of two Kinds
    assert not domain.covers([Sensitivity.PII, Sensitivity.RESTRICTED], [KIND])
    assert Clearance.full().covers(Sensitivity, [])
    variants = {c.variant for c in (nothing, domain, person, Clearance.full())}
    assert len(variants) == 4
    assert nothing.variant == ""


def test_masking_replaces_or_omits_values() -> None:
    masked = Masking(Clearance()).kind_values(PERSON, VALUES)
    assert masked == {"code": "P-1", "name": "Ada", "phone": "***", "secret": "***"}
    omitted = Masking(Clearance(), SensitivitySettings(omit=True)).kind_values(PERSON, VALUES)
    assert omitted == {"code": "P-1", "name": "Ada"}
    cleared = Masking(Clearance(everywhere=frozenset({Sensitivity.PII})))
    assert cleared.kind_values(PERSON, VALUES)["phone"] == "555"
    assert cleared.kind_values(PERSON, VALUES)["secret"] == "***"  # also restricted
    assert Masking(Clearance.full()).kind_values(PERSON, VALUES) == {
        "code": "P-1",
        "name": "Ada",
        "phone": "555",
        "secret": "x",
    }


def _plan(document: dict[str, Any]) -> Any:
    return resolve_query(
        Query.model_validate({"kind": "Person", **document}), SCHEMA, QueryLimits()
    )


@pytest.mark.parametrize(
    "document",
    [
        {"where": {"attr": "phone", "op": "eq", "value": "555"}},
        {"where": {"not": {"attr": "phone", "op": "exists"}}},
        {"order_by": [{"attr": "phone"}]},
        {"aggregate": {"group_by": "phone"}},
    ],
)
def test_comparing_masked_values_needs_the_clearance(document: dict[str, Any]) -> None:
    plan = _plan(document)
    with pytest.raises(ProblemError) as refused:
        require_comparable_plan(SCHEMA, Masking(Clearance()), plan, "/body")
    assert refused.value.status_code == 403
    require_comparable_plan(SCHEMA, Masking(Clearance.full()), plan, "/body")


def test_vault_values_never_compare() -> None:
    plan = _plan({"where": {"attr": "email", "op": "eq", "value": "a@b.c"}})
    with pytest.raises(ProblemError) as refused:
        require_comparable_plan(SCHEMA, Masking(Clearance.full()), plan, "/body")
    assert refused.value.status_code == 422
    require_comparable_plan(
        SCHEMA,
        Masking(Clearance()),
        _plan({"where": {"attr": "name", "op": "eq", "value": "Ada"}}),
        "/body",
    )


def test_conflicts_over_masked_values_are_masked() -> None:
    payload = {
        "partition": kind_partition(KIND),
        "attribute": PHONE,
        "base": "1",
        "ours": "2",
        "theirs": "3",
        "message": "phone 2 against 3",
        "details": {"value": "2"},
    }
    masked = mask_conflict(payload, SCHEMA, Masking(Clearance()))
    assert (masked["base"], masked["ours"], masked["theirs"]) == ("***",) * 3
    assert "2" not in masked["message"]
    assert masked["details"] == {}
    assert mask_conflict(payload, SCHEMA, Masking(Clearance.full())) == payload
    record_level = {
        "partition": kind_partition(KIND),
        "attribute": None,
        "ours": {"values": {NAME: "Ada", PHONE: "555"}},
        "theirs": None,
    }
    shown = mask_conflict(record_level, SCHEMA, Masking(Clearance()))
    assert shown["ours"] == {"values": {NAME: "Ada", PHONE: "***"}}
    unknown = {**payload, "attribute": "att_01JB8Z00000000000000000099"}
    assert mask_conflict(unknown, SCHEMA, Masking(Clearance()))["ours"] == "***"


class FakeAuthorizer:
    """Domain-wide permissions and Kind-narrowed ones (by api name), as `Authorizer` answers."""

    def __init__(self, wide: set[Permission], kinds: dict[Permission, set[str]] | None = None):
        self.wide = wide
        self.kinds = kinds or {}

    async def allows(
        self, principal: Any, permission: Permission, domain: str, context: Any
    ) -> bool:
        assert context.branch is None  # branch-narrowed grants never clear
        return permission in self.wide

    async def granted_kinds(
        self, principal: Any, permission: Permission, domain: str
    ) -> frozenset[str]:
        return frozenset(self.kinds.get(permission, set()))


def _without_tags(schema: Schema) -> Schema:
    kind = schema.kinds[KIND]
    plain = {k: a.model_copy(update={"sensitivity": ()}) for k, a in kind.attributes.items()}
    return schema.model_copy(
        update={"kinds": {KIND: kind.model_copy(update={"attributes": plain})}}
    )


async def test_tags_of_the_default_branch_protect_every_read() -> None:
    # An older Revision, or a branch where the tag was removed, has a schema without it.
    untagged = _without_tags(SCHEMA)
    authorizer: Any = FakeAuthorizer(set())
    masking = await masking_for(authorizer, None, "people", SCHEMA, SensitivitySettings())  # type: ignore[arg-type]
    assert masking.kind_values(untagged.kinds[KIND], VALUES)["phone"] == "***"
    assert masking.kind_values(SCHEMA.kinds[KIND], VALUES)["phone"] == "***"


async def test_kind_clearances_follow_kind_ids_of_the_default_branch() -> None:
    authorizer: Any = FakeAuthorizer(set(), {Permission.PII_READ: {"Person"}})
    clearance = await clearance_of(authorizer, None, "people", SCHEMA)  # type: ignore[arg-type]
    assert clearance.kinds == {Sensitivity.PII: frozenset({KIND})}
    # On a branch where another Kind took the name "Person", the clearance stays with KIND.
    renamed = SCHEMA.model_copy(
        update={"kinds": {KIND: SCHEMA.kinds[KIND].model_copy(update={"api_name": "Former"})}}
    )
    masking = Masking(clearance)
    assert masking.kind_values(renamed.kinds[KIND], VALUES)["phone"] == "555"
    other = SCHEMA.kinds[KIND].model_copy(update={"id": "knd_01JB8Z00000000000000000077"})
    assert masking.kind_values(other, VALUES)["phone"] == "***"
    # A name the default branch does not know clears nothing.
    stranger: Any = FakeAuthorizer(set(), {Permission.PII_READ: {"Nobody"}})
    assert (await clearance_of(stranger, None, "people", SCHEMA)).kinds == {}  # type: ignore[arg-type]


async def test_removing_tags_from_the_default_branch_needs_the_clearance() -> None:
    assert lowered_tags(SCHEMA, _without_tags(SCHEMA)) == {Sensitivity.PII, Sensitivity.RESTRICTED}
    removed = SCHEMA.model_copy(update={"kinds": {}})
    assert lowered_tags(SCHEMA, removed) == {Sensitivity.PII, Sensitivity.RESTRICTED}
    assert lowered_tags(_without_tags(SCHEMA), SCHEMA) == frozenset()
    modeler: Any = FakeAuthorizer({Permission.PII_READ})
    with pytest.raises(ProblemError) as refused:
        await require_lowering(modeler, None, "people", SCHEMA, _without_tags(SCHEMA))  # type: ignore[arg-type]
    assert refused.value.status_code == 403
    cleared: Any = FakeAuthorizer({Permission.PII_READ, Permission.RESTRICTED_READ})
    await require_lowering(cleared, None, "people", SCHEMA, _without_tags(SCHEMA))  # type: ignore[arg-type]


def test_conflict_maps_of_unknown_attributes_are_masked() -> None:
    record_level = {
        "partition": kind_partition(KIND),
        "attribute": None,
        "ours": {"values": {NAME: "Ada", PHONE: "555"}},
    }
    # Target branch not materialized: no schema, so every value is masked.
    assert mask_conflict(record_level, None, Masking(Clearance()))["ours"] == {
        "values": {NAME: "***", PHONE: "***"}
    }
    assert mask_conflict(record_level, None, Masking(Clearance.full()))["ours"] == {
        "values": {NAME: "Ada", PHONE: "555"}
    }


def test_extraction_items_mask_stored_values() -> None:
    shown = _shown(SCHEMA, Masking(Clearance()))
    subject = {"kind": KIND, "attribute": PHONE, "value": "556", "current": "555"}
    assert _scrub(subject, shown) == {**subject, "value": "***", "current": "***"}
    record = {"kind": KIND, "values": {NAME: "Ada", PHONE: "555", "att_unknown": "x"}}
    # Ids the schema does not know are masked too, unless the caller is cleared for all.
    assert _scrub(record, shown)["values"] == {NAME: "Ada", PHONE: "***", "att_unknown": "***"}
    assert _scrub({"value": "557"}, shown, PHONE) == {"value": "***"}
    full = _shown(SCHEMA, Masking(Clearance.full()))
    assert _scrub(record, full) == record


def test_recorded_parameters_leave_sensitive_values_out() -> None:
    create = resolve(SCHEMA, "create_person")
    recorded = recorded_parameters(SCHEMA, create, {"code": "P-1", "name": "Ada", "phone": "555"})
    assert recorded == {"code": "P-1", "name": "Ada", "phone": "***"}
    plain = _without_tags(SCHEMA)
    assert recorded_parameters(plain, resolve(plain, "create_person"), {"phone": "555"}) == {
        "phone": "555"
    }


def _with_copy(schema: Schema, tagged: bool) -> Schema:
    """`schema` plus `phone_copy`, derived from `phone`; `phone` untagged unless `tagged`."""
    copy_id = "att_01JB8Z00000000000000000088"
    derivation = Derivation(
        id="drv_01JB8Z00000000000000000088",
        api_name="phone_copy",
        definition=ExpressionDerivation(kind=KIND, attribute=copy_id, expression="phone"),
    )
    kind = schema.kinds[KIND]
    phone = kind.attributes[PHONE].model_copy(
        update={"sensitivity": (Sensitivity.PII,) if tagged else ()}
    )
    copy = _attribute(copy_id, "phone_copy", derivation_id=derivation.id)
    attributes = {**kind.attributes, PHONE: phone, copy_id: copy}
    return schema.model_copy(
        update={
            "kinds": {KIND: kind.model_copy(update={"attributes": attributes})},
            "derivations": {derivation.id: derivation},
        }
    )


async def test_a_derived_copy_on_a_branch_is_masked_like_its_source() -> None:
    # The branch forked before the default branch tagged phone, and derives an untagged copy.
    branch = _with_copy(SCHEMA, tagged=False)
    reference = _with_copy(SCHEMA, tagged=True)
    authorizer: Any = FakeAuthorizer(set())
    base = await masking_for(authorizer, None, "people", reference, SensitivitySettings())  # type: ignore[arg-type]
    shown = base.over(branch).kind_values(
        branch.kinds[KIND], VALUES, derived={"att_01JB8Z00000000000000000088": "555"}
    )
    assert (shown["phone"], shown["phone_copy"]) == ("***", "***")


async def test_disclosure_routes_json_variants_to_the_json_path() -> None:
    disclosure = Disclosure(None, "people", (), Masking(Clearance()), None)  # type: ignore[arg-type]

    async def fake_shown(tokens: set[str]) -> dict[str, Any]:
        return dict.fromkeys(tokens, "***")

    disclosure._shown = fake_shown  # type: ignore[method-assign]
    reference = b'{"v":{"$vault":"vlt_' + b"a" * 32 + b'"}}'
    assert await disclosure.send(reference, "application/problem+json") == b'{"v":"***"}'
    lines = reference + b"\n" + reference + b"\n"
    assert await disclosure.send(lines, "application/x-ndjson") == b'{"v":"***"}\n{"v":"***"}\n'
    markdown = b'| a | {"$vault": "vlt_' + b"a" * 32 + b'"} |'
    assert await disclosure.send(markdown, "text/markdown") == b"| a | \\*\\*\\* |"


def _titled() -> Schema:
    """SCHEMA whose Person title is the sensitive phone."""
    kind = SCHEMA.kinds[KIND].model_copy(update={"title_attribute": PHONE})
    return SCHEMA.model_copy(update={"kinds": {KIND: kind}})


@pytest.mark.parametrize(
    "document",
    [
        {"where": {"attr": "title", "op": "eq", "value": "555"}},
        {"order_by": [{"attr": "title"}]},
        {"aggregate": {"group_by": "title"}},
    ],
)
def test_comparing_titles_needs_the_clearance_of_the_title_attribute(
    document: dict[str, Any],
) -> None:
    schema = _titled()
    plan = resolve_query(
        Query.model_validate({"kind": "Person", **document}), schema, QueryLimits()
    )
    with pytest.raises(ProblemError) as refused:
        require_comparable_plan(schema, Masking(Clearance()), plan, "/body")
    assert refused.value.status_code == 403
    require_comparable_plan(schema, Masking(Clearance.full()), plan, "/body")
    require_comparable_plan(SCHEMA, Masking(Clearance()), _plan(document), "/body")


def test_cached_responses_depend_on_the_default_branch_tags() -> None:
    plain = Masking(Clearance())
    tagged = Masking(Clearance(), reference={PHONE: frozenset({Sensitivity.PII})})
    retagged = Masking(Clearance(), reference={PHONE: frozenset({Sensitivity.RESTRICTED})})
    assert len({plain.variant, tagged.variant, retagged.variant}) == 3


def test_evaluation_errors_on_unreadable_values_are_redacted() -> None:
    error = ExprError(
        ExprErrorCode.EVALUATION, "int() cannot parse '555-12'", source="int(record(r).phone)"
    )
    redact = _redactor(SCHEMA, Masking(Clearance()))
    finding = _expression_finding(error, redact)
    assert "555" not in finding.message
    assert finding.details == {}
    shown = _expression_finding(error, _redactor(SCHEMA, Masking(Clearance.full())))
    assert "555-12" in shown.message
    other = ExprError(ExprErrorCode.EVALUATION, "bad name", source="record(r).name")
    assert _expression_finding(other, redact).message.startswith("bad name")


def test_source_run_errors_must_be_asked_for() -> None:
    import inspect

    from lorebase_keeper.routes.sources import _run

    parameter = inspect.signature(_run).parameters["errors"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


def test_snippets_leave_out_hidden_titles() -> None:
    from sqlalchemy import column, table

    from lorebase_core.query import Target
    from lorebase_keeper.reading.search import _snippet_title

    rows = table("r", column("kind_id"), column("title"))
    kind = _titled().kinds[KIND]
    hidden = str(_snippet_title(rows, Target("Person", (kind,)), Masking(Clearance())))
    assert "CASE" in hidden
    plain = str(_snippet_title(rows, Target("Person", (SCHEMA.kinds[KIND],)), Masking(Clearance())))
    assert "CASE" not in plain


def test_agent_writes_mask_with_the_default_branch_tags() -> None:
    from lorebase_keeper.services.operations import execution_masking

    masking = execution_masking(None, _with_copy(SCHEMA, tagged=True)).over(
        _with_copy(SCHEMA, tagged=False)
    )
    assert masking.sensitive(
        SCHEMA.kinds[KIND].attributes[PHONE].model_copy(update={"sensitivity": ()})
    )
    assert masking.clearance == Clearance()


def test_the_redactor_resolves_fields_against_the_addressed_kind() -> None:
    from lorebase_core.model import Operation, OperationParameter

    operation = Operation(
        id="opr_01JB8Z00000000000000000001",
        api_name="touch",
        parameters=(OperationParameter(name="p", type=TypeSpec.ref(KIND), required=True),),
    )
    error = ExprError(ExprErrorCode.EVALUATION, "bad 'Ada'", source="int(record(p).name)")
    redact = _redactor(SCHEMA, Masking(Clearance()), operation)
    assert _expression_finding(error, redact).message.startswith("bad 'Ada'")
    phone = ExprError(ExprErrorCode.EVALUATION, "bad '555'", source="int(record(p).phone)")
    assert "555" not in _expression_finding(phone, redact).message


def test_issues_about_unreadable_values_do_not_tell_their_format() -> None:
    values = {CODE: "P-1", NAME: 1, PHONE: 5, EMAIL: 7, SECRET: "x"}
    issues = validate_record(SCHEMA, Record(kind=KIND, key="P-1", values=values))
    assert {i.attribute: i.code for i in issues} == {
        NAME: IssueCode.VALUE_TYPE,
        PHONE: IssueCode.VALUE_TYPE,
        EMAIL: IssueCode.VALUE_TYPE,
    }
    uncleared = {i.attribute: i for i in generalize_issues(issues, (SCHEMA,), Masking(Clearance()))}
    assert uncleared[NAME].code is IssueCode.VALUE_TYPE  # not sensitive
    for attribute, name in ((PHONE, "phone"), (EMAIL, "email")):
        found = uncleared[attribute]
        original = next(i for i in issues if i.attribute == attribute)
        assert (found.code, found.message, found.details) == (
            IssueCode.VALUE_INVALID,
            f"{name!r} is not valid",
            {},
        )
        assert (found.path, found.kind, found.key) == (original.path, KIND, "P-1")
    cleared = Masking(Clearance(everywhere=frozenset({Sensitivity.PII})))
    assert generalize_issues(issues, (SCHEMA,), cleared) == issues
    # absence tells nothing about the value; an Attribute no schema knows is hidden
    required = Issue(code=IssueCode.VALUE_REQUIRED, message="m", kind=KIND, attribute=PHONE)
    assert generalize_issues([required], (SCHEMA,), Masking(Clearance())) == [required]
    unknown = Issue(code=IssueCode.VALUE_PATTERN, message="m", kind=KIND, attribute="att_x")
    assert generalize_issues([unknown], (SCHEMA,), Masking(Clearance()))[0].code is (
        IssueCode.VALUE_INVALID
    )
    assert generalize_issues([unknown], (SCHEMA,), Masking(Clearance.full())) == [unknown]
