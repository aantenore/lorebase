"""Which filter values a trace keeps: the read reports each field it compares with whether it is
sensitive (`require_comparable`), through Traits, Derivations and keys too."""

from __future__ import annotations

import json
from typing import Any

from lorebase_core.model import Schema
from lorebase_core.model.yaml_schema import load_schema
from lorebase_core.query import Query, QueryLimits, resolve_query
from lorebase_keeper.reading.sensitivity import Clearance, Masking, require_comparable_plan
from lorebase_keeper.tracing import capture
from lorebase_keeper.tracing.config import TraceSettings
from lorebase_keeper.tracing.notes import TraceNotes, note_field, tracing

SCHEMA = """
format: lorebase.schema/v1
domain:
  id: crm
  display_name: CRM
  default_branch: main
kinds:
  Person:
    id: knd_01KF0000000000000000000001
    primary_key: [email]
    title_attribute: full_name
    attributes:
      email:
        id: att_01KF0000000000000000000011
        type: string
        sensitivity: [pii]
      full_name:
        id: att_01KF0000000000000000000012
        type: string
        sensitivity: [pii]
      initials:
        id: att_01KF0000000000000000000013
        type: string
        derivation: initials
    traits:
      - trait: Party
        attribute_map: {name: full_name, label: initials}
  Company:
    id: knd_01KF0000000000000000000002
    primary_key: [code]
    title_attribute: name
    attributes:
      code:
        id: att_01KF0000000000000000000021
        type: string
      name:
        id: att_01KF0000000000000000000022
        type: string
      label:
        id: att_01KF0000000000000000000023
        type: string
    traits:
      - trait: Party
        attribute_map: {name: name, label: label}
traits:
  Party:
    id: trt_01KF0000000000000000000003
    attributes:
      name:
        id: att_01KF0000000000000000000031
        type: string
      label:
        id: att_01KF0000000000000000000032
        type: string
derivations:
  initials:
    id: drv_01KF0000000000000000000004
    definition:
      variant: expression
      kind: Person
      attribute: initials
      expression: record.full_name.substring(0, 1)
"""


def schema() -> Schema:
    return load_schema(SCHEMA)


def recorded(target: str, where: dict[str, Any]) -> Any:
    """The `where` a trace keeps of a query on `target`, after the read checked its fields as
    a caller holding every clearance would (masking reports sensitivity whether cleared or
    not)."""
    compiled = schema()
    masking = Masking(Clearance.full()).over(compiled)
    member = "trait" if compiled.trait_by_api_name(target) is not None else "kind"
    sent = {member: target, "where": where}
    plan = resolve_query(Query.model_validate(sent), compiled, QueryLimits())
    with tracing(TraceNotes(filter_values=True)) as notes:
        require_comparable_plan(compiled, masking, plan, "/body")
    body = json.dumps(sent).encode()
    query = capture.query_of(
        TraceSettings(), params=[], body=body, body_complete=True, plain=notes.plain
    )
    assert query is not None
    return query["body"]["where"]


def test_a_trait_field_mapped_to_a_sensitive_attribute_keeps_no_value() -> None:
    where = {"attr": "name", "op": "eq", "value": "Ann Example"}
    assert recorded("Party", where)["value"] == {"redacted": "string"}  # Person.full_name: pii
    assert recorded("Company", where)["value"] == "Ann Example"  # Company.name: untagged


def test_a_trait_field_mapped_to_a_derivation_of_a_sensitive_attribute_keeps_no_value() -> None:
    where = {"attr": "label", "op": "eq", "value": "A"}
    assert recorded("Party", where)["value"] == {"redacted": "string"}  # derived from pii
    assert recorded("Company", where)["value"] == "A"


def test_key_and_title_follow_their_attributes() -> None:
    key = {"attr": "key", "op": "eq", "value": "ann@example.com"}
    assert recorded("Person", key)["value"] == {"redacted": "string"}  # primary key email: pii
    assert recorded("Company", {**key, "value": "C-1"})["value"] == "C-1"
    title = {"attr": "title", "op": "prefix", "value": "Ann"}
    assert recorded("Person", title)["value"] == {"redacted": "string"}
    assert recorded("Company", title)["value"] == "Ann"


def test_a_name_sensitive_anywhere_in_the_read_is_kept_nowhere() -> None:
    with tracing(TraceNotes(filter_values=True)) as notes:
        note_field("name", sensitive=False)
        note_field("name", sensitive=True)
        note_field("code", sensitive=False)
    assert not notes.plain("name")
    assert notes.plain("code")
    assert not notes.plain("never_resolved")
    with tracing(TraceNotes()) as off:
        note_field("code", sensitive=False)
    assert not off.plain("code")  # capture_filter_values: none, or capture_literals
