"""One scenario per Conflict kind: each produces exactly that Conflict (design 6.9)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from lorebase_core.merge import ConflictKind, MergePolicy, Side
from lorebase_core.model import (
    Cardinality,
    Kind,
    OnDelete,
    Schema,
    TraitImplementation,
)
from lorebase_core.tree import PARTITION_SCHEMA, kind_partition, relation_partition
from lorebase_core.types import TypeSpec

from .support import (
    COUNTRY,
    LABEL,
    NAME,
    NAMED,
    PART,
    RATING,
    SUPPLIED_BY,
    SUPPLIER,
    VAT,
    Edit,
    Fork,
    attempt,
    attribute,
    base_records,
    base_schema,
    fork,
    ident,
    merged,
    named_trait,
    part,
    remove_attribute,
    replace_attribute,
    replace_kind,
    replace_relation,
    supplied_by,
    supplier,
    with_elements,
)

K = ConflictKind


@dataclass(frozen=True)
class Case:
    kind: ConflictKind
    build: Callable[[], Fork]
    partition: str
    key: str | None
    attribute: str | None


def _dv1() -> Fork:
    return fork(
        Edit().upsert(supplier("S-1", "Ours", vat="IT001", country="IT", rating="3")),
        Edit().upsert(supplier("S-1", "Theirs", vat="IT001", country="IT", rating="3")),
    )


def _dv2() -> Fork:
    return fork(Edit().remove(part("P-3")), Edit().upsert(part("P-3", "Renamed")))


def _dv3() -> Fork:
    return fork(Edit().upsert(supplier("S-9", "Ours")), Edit().upsert(supplier("S-9", "Theirs")))


def _sc1() -> Fork:
    schema = base_schema()
    return fork(
        Edit(schema=replace_attribute(schema, SUPPLIER, COUNTRY, description="ours")),
        Edit(schema=replace_attribute(schema, SUPPLIER, COUNTRY, description="theirs")),
    )


def _with_trait(trait_required: bool = False) -> Schema:
    schema = with_elements(base_schema(), traits={NAMED: named_trait(trait_required)})
    return replace_kind(
        schema,
        SUPPLIER,
        traits=(TraitImplementation(trait_id=NAMED, attribute_map={LABEL: NAME}),),
    )


def _sc2() -> Fork:
    schema = with_elements(base_schema(), traits={NAMED: named_trait()})
    changed = named_trait().model_copy(update={"description": "changed"})
    return fork(
        Edit(schema=with_elements(schema, traits={})),
        Edit(schema=with_elements(schema, traits={NAMED: changed})),
        schema=schema,
    )


def _vendor(kind_id: str) -> Kind:
    code, name = ident("att", 900 + int(kind_id[-2:])), ident("att", 950 + int(kind_id[-2:]))
    return Kind(
        id=kind_id,
        api_name="Vendor",
        primary_key=(code,),
        title_attribute=name,
        attributes={
            code: attribute(code, "code", required=True),
            name: attribute(name, "name", required=True),
        },
    )


VENDOR_OURS = ident("knd", 51)
VENDOR_THEIRS = ident("knd", 52)


def _sc3() -> Fork:
    schema = base_schema()
    return fork(
        Edit(
            schema=with_elements(schema, kinds={**schema.kinds, VENDOR_OURS: _vendor(VENDOR_OURS)})
        ),
        Edit(
            schema=with_elements(
                schema, kinds={**schema.kinds, VENDOR_THEIRS: _vendor(VENDOR_THEIRS)}
            )
        ),
    )


def _xs1() -> Fork:
    schema = base_schema()
    without_part = with_elements(schema, kinds={SUPPLIER: schema.kinds[SUPPLIER]}, relations={})
    return fork(
        Edit().upsert(part("P-1", "Changed", qty="10")),
        Edit(schema=without_part).remove(
            part("P-1"),
            part("P-2"),
            part("P-3"),
            supplied_by("P-1", "S-1"),
            supplied_by("P-2", "S-2"),
        ),
    )


def _xs2() -> Fork:
    return fork(
        Edit(schema=remove_attribute(base_schema(), SUPPLIER, RATING)).upsert(
            supplier("S-1", vat="IT001", country="IT")
        ),
        Edit().upsert(supplier("S-2", vat="IT002", country="IT", rating="5")),
    )


def _xs3() -> Fork:
    retyped = replace_attribute(base_schema(), SUPPLIER, COUNTRY, type=TypeSpec.enum("IT", "DE"))
    return fork(
        Edit(schema=retyped),
        Edit().upsert(supplier("S-2", vat="IT002", country="FR")),
    )


def _xs4(default: str | None = None) -> Fork:
    required = replace_attribute(base_schema(), SUPPLIER, COUNTRY, required=True, default=default)
    return fork(
        Edit(schema=required).upsert(supplier("S-3", vat="IT003", country="IT")),
        Edit().upsert(supplier("S-9")),
    )


def _xs5() -> Fork:
    from lorebase_core.types import Constraints

    tightened = replace_attribute(
        base_schema(), SUPPLIER, VAT, constraints=Constraints(pattern="^IT")
    )
    return fork(Edit(schema=tightened), Edit().upsert(supplier("S-3", vat="DE999")))


def _xs6() -> Fork:
    return fork(Edit().upsert(supplied_by("P-3", "S-1")), Edit().upsert(supplied_by("P-3", "S-2")))


def _xs7() -> Fork:
    required = replace_relation(base_schema(), SUPPLIED_BY, required_from=True)
    return fork(
        Edit(schema=required).upsert(supplied_by("P-3", "S-3")),
        Edit().upsert(part("P-9")),
    )


def _xs8() -> Fork:
    return fork(
        Edit().upsert(supplier("S-1", vat="IT777", country="IT", rating="3")),
        Edit().upsert(supplier("S-2", vat="IT777", country="IT")),
    )


def _xs9() -> Fork:
    restrict = replace_relation(base_schema(), SUPPLIED_BY, on_delete=OnDelete.RESTRICT)
    return fork(
        Edit().remove(supplier("S-3")),
        Edit().upsert(supplied_by("P-3", "S-3")),
        schema=restrict,
    )


def _xs10() -> Fork:
    schema = _with_trait()
    return fork(
        Edit(schema=with_elements(schema, traits={NAMED: named_trait(True)})),
        Edit(schema=replace_attribute(schema, SUPPLIER, NAME, required=False)),
        schema=schema,
    )


def _xs11() -> Fork:
    data = [
        supplier("S-1", vat="IT001", rating="3"),
        supplier("S-2", vat="IT002"),
        supplier("S-3", vat="IT003"),
        *base_records()[3:],
    ]
    return fork(
        Edit(schema=remove_attribute(base_schema(), SUPPLIER, COUNTRY)),
        Edit(schema=replace_kind(base_schema(), SUPPLIER, title_attribute=COUNTRY)),
        data=data,
    )


SUPPLIERS = kind_partition(SUPPLIER)
PARTS = kind_partition(PART)
LINKS = relation_partition(SUPPLIED_BY)

CASES = [
    Case(K.ATTRIBUTE_VALUE, _dv1, SUPPLIERS, "S-1", NAME),
    Case(K.DELETE_MODIFY, _dv2, PARTS, "P-3", None),
    Case(K.ADD_ADD, _dv3, SUPPLIERS, "S-9", NAME),
    Case(K.SCHEMA_PROPERTY, _sc1, PARTITION_SCHEMA, SUPPLIER, f"attributes.{COUNTRY}.description"),
    Case(K.SCHEMA_DELETE_MODIFY, _sc2, PARTITION_SCHEMA, NAMED, None),
    Case(K.NAMING, _sc3, PARTITION_SCHEMA, VENDOR_THEIRS, "api_name"),
    Case(K.ORPHANED_DATA, _xs1, PARTS, None, None),
    Case(K.ORPHANED_VALUE, _xs2, SUPPLIERS, None, RATING),
    Case(K.TYPE_COERCION, _xs3, SUPPLIERS, None, COUNTRY),
    Case(K.MISSING_VALUE, _xs4, SUPPLIERS, None, COUNTRY),
    Case(K.CONSTRAINT, _xs5, SUPPLIERS, None, VAT),
    Case(K.CARDINALITY, _xs6, LINKS, None, "from"),
    Case(K.REQUIRED_RELATION, _xs7, LINKS, None, None),
    Case(K.UNIQUENESS, _xs8, SUPPLIERS, None, VAT),
    Case(K.DANGLING_LINK, _xs9, LINKS, None, None),
    Case(K.TRAIT_CONFORMANCE, _xs10, PARTITION_SCHEMA, SUPPLIER, f"traits.0.attribute_map.{LABEL}"),
    Case(K.BROKEN_REFERENCE, _xs11, PARTITION_SCHEMA, SUPPLIER, "title_attribute"),
]


def test_every_kind_has_a_case() -> None:
    assert {case.kind for case in CASES} == set(ConflictKind)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.kind.value)
def test_case_produces_exactly_its_conflict(case: Case) -> None:
    result = attempt(case.build().merge())
    assert [c.kind for c in result.conflicts] == [case.kind]
    conflict = result.conflicts[0]
    assert (conflict.partition, conflict.key, conflict.attribute) == (
        case.partition,
        case.key,
        case.attribute,
    )
    assert conflict.choices
    assert len(conflict.fingerprint) == 64


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.kind.value)
def test_conflict_set_is_symmetric(case: Case) -> None:
    scenario = case.build()
    forward = attempt(scenario.merge())
    backward = attempt(scenario.reverse())
    assert _labels(forward.conflicts) == _labels(backward.conflicts, swap=True)


def _labels(conflicts: Any, *, swap: bool = False) -> set[tuple[object, ...]]:
    labels = set()
    for c in conflicts:
        ours, theirs = (c.theirs, c.ours) if swap else (c.ours, c.theirs)
        labels.add(
            (
                c.kind,
                c.partition,
                c.key,
                c.attribute,
                repr(c.base),
                repr(ours),
                repr(theirs),
                c.count,
            )
        )
    return labels


def test_grouped_conflicts_carry_count_and_sample() -> None:
    scenario = fork(
        Edit(schema=remove_attribute(base_schema(), SUPPLIER, RATING)).upsert(
            supplier("S-1", vat="IT001", country="IT")
        ),
        Edit().upsert(*(supplier(f"S-{n}", vat=f"IT{n:03d}", rating=str(n)) for n in range(1, 16))),
    )
    conflict = attempt(scenario.merge(policy=MergePolicy(sample_size=4))).conflicts[0]
    assert conflict.kind is K.ORPHANED_VALUE
    assert conflict.count == 15
    assert len(conflict.sample) == 4
    assert conflict.grouped


def test_new_required_attribute_takes_its_default() -> None:
    scenario = _xs4(default="IT")
    result = merged(scenario.merge())
    assert values_of(scenario, result.root, "S-9")[COUNTRY] == "IT"
    assert [a.resolver for a in result.auto_resolutions] == ["policy:default"]
    assert result.auto_resolutions[0].kind is K.MISSING_VALUE


def test_dangling_link_of_cascade_relation_is_dropped() -> None:
    scenario = fork(Edit().remove(supplier("S-3")), Edit().upsert(supplied_by("P-3", "S-3")))
    result = merged(scenario.merge())
    assert not scenario.repo.has_link(result.root, SUPPLIED_BY, "P-3", "S-3")
    assert [(a.kind, a.resolver) for a in result.auto_resolutions] == [
        (K.DANGLING_LINK, "policy:cascade_dangling")
    ]


def test_dangling_link_conflicts_without_cascade_policy() -> None:
    scenario = fork(Edit().remove(supplier("S-3")), Edit().upsert(supplied_by("P-3", "S-3")))
    result = attempt(scenario.merge(policy=MergePolicy(cascade_dangling=False)))
    assert [c.kind for c in result.conflicts] == [K.DANGLING_LINK]


def test_tightened_cardinality_conflicts_with_links_added_on_the_other_side() -> None:
    one_to_one = replace_relation(base_schema(), SUPPLIED_BY, cardinality=Cardinality.ONE_TO_ONE)
    scenario = fork(Edit(schema=one_to_one), Edit().upsert(supplied_by("P-3", "S-1")))
    result = attempt(scenario.merge())
    assert [(c.kind, c.attribute, c.sample) for c in result.conflicts] == [
        (K.CARDINALITY, "to", ("S-1",))
    ]


def test_checked_casts_can_be_disallowed() -> None:
    retyped = replace_attribute(base_schema(), SUPPLIER, COUNTRY, type=TypeSpec.enum("IT", "DE"))
    scenario = fork(Edit(schema=retyped), Edit().upsert(supplier("S-2", vat="IT002", country="DE")))
    assert isinstance(merged(scenario.merge()).root, bytes)
    result = attempt(scenario.merge(policy=MergePolicy(allow_checked_casts=False)))
    assert [c.kind for c in result.conflicts] == [K.TYPE_COERCION]


def test_prefer_policy_resolves_value_conflicts() -> None:
    scenario = _dv1()
    result = merged(scenario.merge(policy=MergePolicy(prefer=Side.THEIRS)))
    assert values_of(scenario, result.root, "S-1")[NAME] == "Theirs"
    assert [a.resolver for a in result.auto_resolutions] == ["policy:prefer"]
    per_attribute = MergePolicy(prefer_attributes={NAME: Side.OURS})
    root = merged(scenario.merge(policy=per_attribute)).root
    assert values_of(scenario, root, "S-1")[NAME] == "Ours"


def values_of(scenario: Fork, root: bytes, key: str) -> dict[str, Any]:
    values = scenario.repo.values(root, SUPPLIER, key)
    assert values is not None
    return values
