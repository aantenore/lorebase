"""Built-in Operations and the execution rules, with an in-memory Record reader."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import JsonValue

from lorebase_core.model import (
    Operation,
    Precondition,
    ReviewMode,
    ReviewPolicy,
    Schema,
    load_schema,
)
from lorebase_core.validate import LinkDelete, LinkUpsert, RecordDelete, RecordUpsert
from lorebase_keeper.errors import ProblemError
from lorebase_keeper.services.config import OperationSettings
from lorebase_keeper.services.operation_catalog import (
    IMPORT_LINKS,
    IMPORT_RECORDS,
    PARAMETER_INVALID,
    PARAMETER_REQUIRED,
    PARAMETER_UNKNOWN,
    RECORD_EXISTS,
    Action,
    bind,
    builtin_definition,
    edit_document,
    import_rows,
    plan_edits,
)
from lorebase_keeper.services.operations import (
    expressions_gate,
    parameters_hash,
    resolve,
    review_required,
)

SUPPLY_SCHEMA = Path(__file__).resolve().parents[3] / "examples" / "supply" / "schema.yaml"
OPERATION_ID = "opr_01KEZF7C00000000000000000A"


@pytest.fixture(scope="module")
def schema() -> Schema:
    document: dict[str, Any] = yaml.safe_load(SUPPLY_SCHEMA.read_text())
    del document["operations"]
    del document["derivations"]["single_sourced"]
    del document["kinds"]["Part"]["attributes"]["single_sourced"]
    return load_schema(yaml.safe_dump(document))


class FakeRecords:
    """Records by Kind id and key, as the materialized head would return them."""

    def __init__(self, records: Mapping[tuple[str, str], Mapping[str, JsonValue]] = {}) -> None:
        self.records = dict(records)

    async def __call__(self, kind: str, keys: Sequence[str]) -> dict[str, Mapping[str, JsonValue]]:
        return {k: self.records[(kind, k)] for k in keys if (kind, k) in self.records}


SUPPLIER_ROW: dict[str, JsonValue] = {
    "supplier_code": "S-1001",
    "legal_name": "Kavsen Alloys Sp. z o.o.",
    "country": "PL",
    "tier": "1",
    "risk_score": 32,
    "onboarded_on": "2024-02-26",
}


def test_builtin_catalog_follows_the_schema(schema: Schema) -> None:
    create = builtin_definition(schema, "create_supplier")
    assert create is not None
    assert create.action is Action.CREATE
    names = {p.name: p for p in create.parameters}
    assert names["supplier_code"].required
    assert not names["city"].required
    update = builtin_definition(schema, "update_purchase_order")
    assert update is not None
    assert update.parameters[0].name == "key"
    assert not any(p.required for p in update.parameters[1:])
    link = builtin_definition(schema, "link_supplied_by")
    assert link is not None
    assert [p.name for p in link.parameters][:2] == ["from", "to"]
    assert builtin_definition(schema, "link_issued_to") is None  # foreign key backed
    assert builtin_definition(schema, IMPORT_RECORDS) is not None
    assert builtin_definition(schema, "create_nothing") is None
    part = builtin_definition(schema, "create_part")
    assert part is not None
    assert "supplier_count" not in {p.name for p in part.parameters}  # derived
    assert create.document()["builtin"] is True


def test_binding_reports_unknown_missing_and_invalid(schema: Schema) -> None:
    create = builtin_definition(schema, "create_supplier")
    assert create is not None
    bound = bind(create, {"supplier_code": "S-1", "risk_score": "high", "colour": "red"})
    codes = {(f.code, f.path) for f in bound.findings}
    assert (PARAMETER_UNKNOWN, ("parameters", "colour")) in codes
    assert (PARAMETER_REQUIRED, ("parameters", "legal_name")) in codes
    assert (PARAMETER_INVALID, ("parameters", "risk_score")) in codes


async def test_create_mints_the_natural_key_and_refuses_duplicates(schema: Schema) -> None:
    create = builtin_definition(schema, "create_supplier")
    assert create is not None
    bound = bind(create, SUPPLIER_ROW)
    assert bound.findings == []
    plan = await plan_edits(create, bound.values, FakeRecords())
    [change] = plan.changes
    assert isinstance(change, RecordUpsert)
    assert change.record.key == "S-1001"
    assert change.record.values["att_01KEZF7C00NK9JAYQ84CSTKF4C"] == "32"  # canonical integer
    existing = FakeRecords({(change.record.kind, "S-1001"): {}})
    again = await plan_edits(create, bound.values, existing)
    assert [f.code for f in again.findings] == [RECORD_EXISTS]
    assert edit_document(schema, change)["values"]["risk_score"] == "32"


async def test_update_merges_and_null_removes(schema: Schema) -> None:
    update = builtin_definition(schema, "update_supplier")
    assert update is not None
    kind = schema.kind_by_api_name("Supplier")
    assert kind is not None
    city = kind.attribute_by_api_name("city")
    country = kind.attribute_by_api_name("country")
    assert city is not None
    assert country is not None
    records = FakeRecords({(kind.id, "S-1001"): {city.id: "Poznan", country.id: "PL"}})
    bound = bind(update, {"key": "S-1001", "city": None, "country": "DE"})
    plan = await plan_edits(update, bound.values, records)
    [change] = plan.changes
    assert isinstance(change, RecordUpsert)
    assert change.record.values == {country.id: "DE"}
    missing = await plan_edits(update, bind(update, {"key": "S-9"}).values, records)
    assert [f.code for f in missing.findings] == ["record.not_found"]


async def test_delete_link_and_unlink(schema: Schema) -> None:
    delete = builtin_definition(schema, "delete_supplier")
    link = builtin_definition(schema, "link_supplied_by")
    unlink = builtin_definition(schema, "unlink_supplied_by")
    assert delete
    assert link
    assert unlink
    reader = FakeRecords()
    deleted = await plan_edits(delete, bind(delete, {"key": "S-1"}).values, reader)
    assert isinstance(deleted.changes[0], RecordDelete)
    params = {"from": "P-10001", "to": "S-1001", "is_primary": True, "lead_time_days": 48}
    linked = await plan_edits(link, bind(link, params).values, reader)
    assert isinstance(linked.changes[0], LinkUpsert)
    unlinked = await plan_edits(unlink, bind(unlink, {"from": "P-1", "to": "S-1"}).values, reader)
    assert isinstance(unlinked.changes[0], LinkDelete)


def test_import_rows_locate_bad_rows(schema: Schema) -> None:
    definition = builtin_definition(schema, IMPORT_RECORDS)
    assert definition is not None
    rows: list[JsonValue] = [SUPPLIER_ROW, {**SUPPLIER_ROW, "supplier_code": "S-1002", "tier": "9"}]
    plan = import_rows(schema, definition, {"kind": "Supplier", "rows": rows}, max_rows=10)
    assert [f.path for f in plan.findings] == [("parameters", "rows", 1, "tier")]
    good = import_rows(schema, definition, {"kind": "Supplier", "rows": rows[:1]}, max_rows=10)
    assert len(good.changes) == 1
    limited = import_rows(schema, definition, {"kind": "Supplier", "rows": rows}, max_rows=1)
    assert limited.findings[0].code == "parameter.limit_exceeded"
    unknown = import_rows(schema, definition, {"kind": "Nope", "rows": []}, max_rows=1)
    assert unknown.findings[0].path == ("parameters", "kind")
    links = builtin_definition(schema, IMPORT_LINKS)
    assert links is not None
    linked = import_rows(
        schema,
        links,
        {"relation": "supplied_by", "rows": [{"from": "P-10001", "to": "S-1001"}]},
        max_rows=10,
    )
    assert isinstance(linked.changes[0], LinkUpsert)


def _custom(**fields: Any) -> Operation:
    return Operation(id=OPERATION_ID, api_name="flag_supplier", description="Flag.", **fields)


def test_preconditions_are_refused_unless_skipping_is_allowed() -> None:
    operation = _custom(preconditions=(Precondition(expression="true", message="m"),))
    with pytest.raises(ProblemError) as raised:
        expressions_gate(operation, OperationSettings())
    assert raised.value.problem.code == "expressions-not-enabled"
    warnings = expressions_gate(operation, OperationSettings(skip_expression_preconditions=True))
    assert warnings[0].severity == "warning"


def test_steps_and_expression_review_policies_are_refused() -> None:
    policy = ReviewPolicy(mode=ReviewMode.EXPRESSION, expression="true")
    with pytest.raises(ProblemError):
        expressions_gate(_custom(review_policy=policy), OperationSettings())


def test_review_policy(schema: Schema) -> None:
    builtin = resolve(schema, "create_supplier")
    settings = OperationSettings()
    assert review_required(builtin, protected=True, settings=settings)
    assert not review_required(builtin, protected=False, settings=settings)
    direct = OperationSettings(direct_commit_on_protected=True)
    assert not review_required(builtin, protected=True, settings=direct)
    always = schema.model_copy(
        update={
            "operations": {
                OPERATION_ID: _custom(review_policy=ReviewPolicy(mode=ReviewMode.ALWAYS))
            }
        }
    )
    assert review_required(resolve(always, "flag_supplier"), protected=False, settings=direct)
    with pytest.raises(ProblemError):
        resolve(schema, "no_such_operation")


def test_parameters_hash_is_stable() -> None:
    first = parameters_hash("create_supplier", "work", {"a": 1, "b": [1, 2]})
    assert first == parameters_hash("create_supplier", "work", {"b": [1, 2], "a": 1})
    assert first != parameters_hash("create_supplier", "main", {"a": 1, "b": [1, 2]})
