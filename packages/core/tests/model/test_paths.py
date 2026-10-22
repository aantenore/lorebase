"""Path Derivations as virtual Relations: resolution, name lookups and the shared walk."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from lorebase_core.expr import (
    ExprLimits,
    LinkedRecord,
    MemoryResolver,
    derivation_reads,
    links_read,
)
from lorebase_core.expr.compiler import compile, run
from lorebase_core.expr.context import activation
from lorebase_core.expr.environment import link_fields, record_environment
from lorebase_core.expr.sensitivity import effective_tags, operation_reads
from lorebase_core.expr.types import INT
from lorebase_core.model import (
    Derivation,
    ExpressionDerivation,
    LinkDirection,
    OperationParameter,
    PathDerivation,
    PathStep,
    Record,
    Schema,
    schema_from_document,
    schema_to_document,
)
from lorebase_core.model.attribute import Sensitivity
from lorebase_core.model.paths import bounded, path_relation, walk
from lorebase_core.query import (
    Query,
    QueryLimits,
    plan_kinds,
    resolve_hop,
    resolve_query,
    target_of_kind,
)
from lorebase_core.types import TypeSpec
from lorebase_core.validate import IssueCode, validate_schema

from .supply import (
    CONTRACT,
    CONTRACT_VALUE,
    MADE_AT,
    PART,
    PLANT,
    SUPPLIED_BY,
    SUPPLIER,
    SUPPLIER_COUNT,
    SUPPLIER_COUNT_DRV,
    SUPPLIER_COUNTRY,
    WITH_SUPPLIER,
    flag_operation,
    ident,
    supply_schema,
)

NOW = datetime(2026, 10, 21, 9, 30, tzinfo=UTC)
PART_CONTRACTS = ident("drv", 50)
BROKEN = ident("drv", 51)


def path(
    derivation_id: str, name: str, *steps: PathStep, inverse: str | None = None, **extra: bool
) -> Derivation:
    return Derivation(
        id=derivation_id,
        api_name=name,
        definition=PathDerivation(steps=steps, inverse_api_name=inverse, **extra),
    )


def schema_with(*derivations: Derivation) -> Schema:
    schema = supply_schema()
    return schema.model_copy(
        update={"derivations": {**schema.derivations, **{d.id: d for d in derivations}}}
    )


def part_contracts(**extra: bool) -> Derivation:
    return path(
        PART_CONTRACTS,
        "contracts_of_suppliers",
        PathStep(relation=SUPPLIED_BY),
        PathStep(relation=WITH_SUPPLIER, direction=LinkDirection.INVERSE),
        inverse="parts_under_contract",
        **extra,
    )


def test_a_path_resolves_to_a_virtual_relation() -> None:
    schema = schema_with(part_contracts(materialized=True))
    found = path_relation(schema.derivations[PART_CONTRACTS], schema.relations)
    assert found is not None
    assert found.materialized
    assert (found.relation.id, found.relation.from_kind, found.relation.to_kind) == (
        PART_CONTRACTS,
        PART,
        CONTRACT,
    )
    assert found.relation.attributes == {}
    assert found.inner_kinds == (schema.relations[SUPPLIED_BY].to_kind,)
    assert schema.path_by_api_name("contracts_of_suppliers") == (found, LinkDirection.FORWARD)
    inverse = schema.path_by_api_name("parts_under_contract")
    assert inverse is not None
    assert inverse[1] is LinkDirection.INVERSE
    assert schema.relation_by_api_name("contracts_of_suppliers") is None


def test_broken_paths_are_left_out() -> None:
    gap = path(BROKEN, "nowhere", PathStep(relation=SUPPLIED_BY), PathStep(relation=MADE_AT))
    schema = schema_with(gap)
    assert path_relation(gap, schema.relations) is None
    assert schema.path_by_api_name("nowhere") is None
    assert BROKEN not in schema.path_relations()


def test_hops_along_a_path_read_the_intermediate_kinds() -> None:
    schema = schema_with(part_contracts())
    part = target_of_kind(schema, "Part")
    assert part is not None
    hop = resolve_hop(schema, part, "contracts_of_suppliers")
    assert not isinstance(hop, str)
    assert hop.path is not None
    assert hop.far.kind_ids == (CONTRACT,)
    assert hop.kinds_read == {"Contract", "Supplier"}
    contract = target_of_kind(schema, "Contract")
    assert contract is not None
    back = resolve_hop(schema, contract, "parts_under_contract")
    assert not isinstance(back, str)
    assert (back.direction, back.far.kind_ids) == (LinkDirection.INVERSE, (PART,))
    assert isinstance(resolve_hop(schema, contract, "contracts_of_suppliers"), str)

    plan = resolve_query(
        Query.model_validate({"kind": "Part", "where": {"via": "contracts_of_suppliers"}}),
        schema,
        QueryLimits(),
    )
    assert plan_kinds(plan) == {"Part", "Supplier", "Contract"}


def lookup(links: Mapping[str, Sequence[str]]) -> object:
    return lambda keys: {k: links[k] for k in keys if k in links}


def test_walk_bounds_later_steps_per_intermediate_record() -> None:
    first = {"a": ["b1", "b2", "b3"], "x": ["b1"]}
    second = {"b1": ["c3", "c1", "c2"], "b2": ["c1"], "b3": ["a"]}
    reached = walk(["a", "x", "lonely"], [lookup(first), lookup(second)], 2)  # type: ignore[list-item]
    # b1 keeps its two smallest far keys whoever reaches it; a cycle back to a is a pair
    assert reached == {"a": {"c1", "c2", "a"}, "x": {"c1", "c2"}, "lonely": set()}
    unbounded = walk(["x"], [lookup(first), lookup(second)], None)  # type: ignore[list-item]
    assert unbounded == {"x": {"c1", "c2", "c3"}}
    assert bounded(["b", "a", "b"], 1) == ["a"]


def test_a_path_may_start_from_the_inverse_of_a_foreign_key() -> None:
    schema = schema_with(
        path(
            BROKEN,
            "plant_contracts",
            PathStep(relation=MADE_AT, direction=LinkDirection.INVERSE),
            PathStep(relation=SUPPLIED_BY),
        )
    )
    found = schema.path_relations()[BROKEN]
    assert found.kinds == (PLANT, PART, schema.relations[SUPPLIED_BY].to_kind)


def aggregate_over_contracts(schema: Schema) -> Schema:
    count = schema.derivations[SUPPLIER_COUNT_DRV]
    definition = count.definition.model_copy(update={"relation": PART_CONTRACTS})
    over_path = count.model_copy(update={"definition": definition})
    return schema.model_copy(
        update={"derivations": {**schema.derivations, SUPPLIER_COUNT_DRV: over_path}}
    )


def test_expressions_and_aggregates_read_a_path_like_a_relation() -> None:
    schema = aggregate_over_contracts(schema_with(part_contracts()))
    assert not [issue for issue in validate_schema(schema) if issue.is_error]
    virtual = schema.path_relations()[PART_CONTRACTS].relation
    assert schema.link_relation(PART_CONTRACTS) == virtual
    assert schema.link_by_api_name("parts_under_contract") == (virtual, LinkDirection.INVERSE)

    assert link_fields(schema, PART)["contracts_of_suppliers"].relation.id == PART_CONTRACTS
    assert "parts_under_contract" in link_fields(schema, CONTRACT)
    program = compile(
        "size(links.contracts_of_suppliers)", record_environment(schema, PART), expected=INT
    )
    (read,) = links_read(program)
    assert (read.relation.id, read.direction) == (PART_CONTRACTS, LinkDirection.FORWARD)
    resolver = MemoryResolver()
    resolver.links[(PART_CONTRACTS, LinkDirection.FORWARD, "P-1")] = [
        LinkedRecord("C-1", {}),
        LinkedRecord("C-2", {}),
    ]
    variables = activation(program.environment, record=Record(kind=PART, key="P-1", values={}))
    assert run(program, variables, now=NOW, resolver=resolver, limits=ExprLimits()) == 2


def test_a_yaml_aggregate_may_name_a_path() -> None:
    schema = schema_with(part_contracts())
    document = schema_to_document(schema)
    document["derivations"]["supplier_count"]["definition"]["relation"] = "contracts_of_suppliers"
    loaded = schema_from_document(document, base=schema)
    assert loaded == aggregate_over_contracts(schema)


def tagged(schema: Schema) -> Schema:
    """`schema` with the Supplier country (crossed by `contracts_of_suppliers`) tagged `pii`
    and the Contract value (at its far end) tagged `confidential`."""
    kinds = dict(schema.kinds)
    for kind_id, attribute_id, tag in (
        (SUPPLIER, SUPPLIER_COUNTRY, Sensitivity.PII),
        (CONTRACT, CONTRACT_VALUE, Sensitivity.CONFIDENTIAL),
    ):
        kind = kinds[kind_id]
        attribute = kind.attributes[attribute_id].model_copy(update={"sensitivity": (tag,)})
        kinds[kind_id] = kind.model_copy(
            update={"attributes": {**kind.attributes, attribute_id: attribute}}
        )
    return schema.model_copy(update={"kinds": kinds})


def with_definition(schema: Schema, definition: object) -> Schema:
    count = schema.derivations[SUPPLIER_COUNT_DRV]
    changed = count.model_copy(update={"definition": definition})
    return schema.model_copy(
        update={"derivations": {**schema.derivations, SUPPLIER_COUNT_DRV: changed}}
    )


def inherited(schema: Schema) -> list[str]:
    return [
        issue.message
        for issue in validate_schema(schema)
        if issue.code is IssueCode.SCHEMA_SENSITIVITY_INHERITED
    ]


def test_a_path_value_carries_the_tags_of_what_it_reads_not_of_what_it_crosses() -> None:
    schema = tagged(aggregate_over_contracts(schema_with(part_contracts())))
    count = schema.derivations[SUPPLIER_COUNT_DRV]
    # a count reads no Attribute: crossing Suppliers only reveals linkage (Kind scope)
    assert derivation_reads(schema, count) == frozenset()
    assert SUPPLIER_COUNT not in effective_tags(schema)
    assert inherited(schema) == []

    definition = count.definition.model_copy(update={"filter": "value > 0"})
    filtered = with_definition(schema, definition)
    assert derivation_reads(filtered, filtered.derivations[SUPPLIER_COUNT_DRV]) == {
        (CONTRACT, CONTRACT_VALUE)
    }
    assert effective_tags(filtered)[SUPPLIER_COUNT] == {Sensitivity.CONFIDENTIAL}
    (message,) = inherited(filtered)
    assert "confidential" in message

    expression = with_definition(
        schema,
        ExpressionDerivation(
            kind=PART,
            attribute=SUPPLIER_COUNT,
            expression="size(links.contracts_of_suppliers.filter(c, c.value > 0))",
        ),
    )
    assert effective_tags(expression)[SUPPLIER_COUNT] == {Sensitivity.CONFIDENTIAL}
    assert inherited(expression)


def test_operation_links_over_a_path_read_the_far_kind() -> None:
    schema = tagged(schema_with(part_contracts()))
    operation = flag_operation().model_copy(
        update={"parameters": (OperationParameter(name="part", type=TypeSpec.ref(PART)),)}
    )

    def read(source: str) -> set[str]:
        return {a.api_name for a in operation_reads(schema, operation, source)}

    typed = 'links(part, "contracts_of_suppliers").exists(c, c.value > 0)'
    assert read(typed) == {"value"}
    assert read('size(links(part, "contracts_of_suppliers")) > 0') == set()
    # an expression that does not type check is resolved from its syntax
    assert read('links(part, "contracts_of_suppliers").value + "x"') == {"value"}
