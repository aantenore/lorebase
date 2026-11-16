"""Proposal and Conflict events: which changes are events, what their payloads tell and who
receives them (no database)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from lorebase_core.model import Schema, Sensitivity
from lorebase_core.tree import PARTITION_SCHEMA, kind_partition, relation_partition
from lorebase_core.types import TypeSpec
from lorebase_store.merging.events import (
    EVERY_TAG,
    RESOLVED_AS_OBSOLETE,
    RESOLVED_BY_RESOLUTION,
    ConflictChange,
    SchemaView,
    capped,
    conflict_changes,
    conflict_payload,
    truncation_payload,
)
from lorebase_store.repositories.merges import ConflictRow, ConflictSummary
from lorebase_store.review_events import (
    AUDIENCE_KEY,
    CONFLICT_OPENED,
    CONFLICT_RESOLVED,
    admitted,
    audience,
    proposal_payload,
    without_audience,
)

from .support import (
    COUNTRY,
    IS_PRIMARY,
    SUPPLIED_BY,
    SUPPLIER,
    attribute,
    domain,
    ident,
    part_kind,
    supplied_by,
    supplier_kind,
)

NOTES = ident("att", 190)
GONE = ident("att", 191)
PART = kind_partition(SUPPLIER)


def conflict(
    fingerprint: str, *, attribute_id: str | None = COUNTRY, resolved: bool = False
) -> ConflictSummary:
    return ConflictSummary.of(
        ConflictRow(
            fingerprint=fingerprint,
            code="DV1",
            partition=PART,
            element_key="S-1",
            attribute_id=attribute_id,
            payload={"base": "IT", "ours": "FR", "theirs": "DE"},
            member_count=1,
            resolution={"choice": "custom", "value": "ES"} if resolved else None,
            resolved_by="usr_reviewer" if resolved else None,
        )
    )


def schema(*, tagged: bool = True) -> Schema:
    kind = supplier_kind()
    notes = attribute(
        NOTES,
        "notes",
        TypeSpec.of("string"),
        sensitivity=(Sensitivity.PII,) if tagged else (),
    )
    kind = kind.model_copy(update={"attributes": {**kind.attributes, NOTES: notes}})
    part = part_kind()
    return Schema(
        domain=domain(),
        kinds={kind.id: kind, part.id: part},
        relations={SUPPLIED_BY: supplied_by()},
    )


def test_an_attempt_is_compared_with_the_previous_one() -> None:
    previous = [conflict("aa"), conflict("bb"), conflict("cc", resolved=True), conflict("dd")]
    current = [
        conflict("aa"),  # still open: nothing
        conflict("bb", resolved=True),  # resolved now
        conflict("cc", resolved=True),  # resolution carried over: nothing
        conflict("ee"),  # new
        conflict("ff", resolved=True),  # resolved in the run that found it: nothing
    ]
    changes = conflict_changes(previous, current)
    assert [(c.event_type, c.conflict.fingerprint, c.reason) for c in changes] == [
        (CONFLICT_RESOLVED, "bb", RESOLVED_BY_RESOLUTION),
        (CONFLICT_OPENED, "ee", None),
        (CONFLICT_RESOLVED, "dd", RESOLVED_AS_OBSOLETE),  # no longer reported
    ]


def test_a_recompute_that_changes_nothing_writes_nothing() -> None:
    rows = [conflict("aa"), conflict("bb", resolved=True)]
    assert conflict_changes(rows, rows) == []
    first = conflict_changes([], rows)
    assert [(c.event_type, c.conflict.fingerprint) for c in first] == [(CONFLICT_OPENED, "aa")]


def test_a_conflict_reported_again_after_its_resolution_opens_again() -> None:
    rejected = conflict("aa")  # open, with its resolution rejected
    changes = conflict_changes([conflict("aa", resolved=True)], [rejected])
    assert [(c.event_type, c.conflict.fingerprint) for c in changes] == [(CONFLICT_OPENED, "aa")]


def test_required_tags_follow_every_schema_and_fail_closed() -> None:
    view = SchemaView.of([schema(tagged=False), schema()], None)
    assert view.required_tags(PART, NOTES) == {Sensitivity.PII}  # tagged on the source side
    assert view.required_tags(PART, COUNTRY) == frozenset()
    assert view.required_tags(PART, None) == frozenset()  # a whole Record
    assert view.required_tags(PART, GONE) == EVERY_TAG  # no schema knows it
    assert view.required_tags(PARTITION_SCHEMA, "type") == frozenset()
    assert view.required_tags("x:unknown", COUNTRY) == EVERY_TAG
    assert view.required_tags(relation_partition(SUPPLIED_BY), IS_PRIMARY) == frozenset()
    # The default branch's tags count on a branch whose schema lacks them.
    untagged = SchemaView.of([schema(tagged=False)], schema())
    assert untagged.required_tags(PART, NOTES) == {Sensitivity.PII}
    assert SchemaView.of([], None).required_tags(PART, COUNTRY) == EVERY_TAG


def test_names_come_from_the_schemas() -> None:
    view = SchemaView.of([schema()], None)
    assert view.names(PART, NOTES) == {"kind": "Supplier", "relation": None, "attribute": "notes"}
    assert view.names(relation_partition(SUPPLIED_BY), IS_PRIMARY) == {
        "kind": None,
        "relation": "supplied_by",
        "attribute": "is_primary",
    }
    assert view.names(PARTITION_SCHEMA, "type") == {
        "kind": None,
        "relation": None,
        "attribute": "type",
    }


def payload_of(row: ConflictSummary, event_type: str, reason: str | None = None) -> dict[str, Any]:
    return conflict_payload(
        "supply",
        ConflictChange(event_type, row, reason),
        SchemaView.of([schema()], None),
        proposal_id="prp_1",
        target="main",
        source="agent/a/t",
        attempt_id="mga_1",
    )


def test_conflict_payloads_name_the_conflict_never_its_values() -> None:
    opened = payload_of(conflict("aa", attribute_id=NOTES), CONFLICT_OPENED)
    assert without_audience(opened) == {
        "domain": "supply",
        "branch": "main",
        "source_branch": "agent/a/t",
        "proposal": "prp_1",
        "attempt": "mga_1",
        "conflict": "aa",
        "code": "DV1",
        "kind": "Supplier",
        "relation": None,
        "attribute": "notes",
        "key": "S-1",
        "count": 1,
    }
    assert opened[AUDIENCE_KEY] == {"branches": ["agent/a/t", "main"], "tags": ["pii"]}
    resolved = payload_of(conflict("aa", resolved=True), CONFLICT_RESOLVED, "resolution")
    assert resolved["choice"] == "custom"
    assert resolved["resolved_by"] == "usr_reviewer"
    assert resolved[AUDIENCE_KEY]["tags"] == []
    for shown in (opened, resolved):
        text = repr(shown)
        for value in ("ES", "FR", "DE"):
            assert value not in text


def test_the_audience_decides_who_receives_an_event() -> None:
    event = {"x": 1, AUDIENCE_KEY: audience(branches=["main", "side"], tags=["pii"])}
    assert admitted(event, branches=None, clearances=("pii",))
    assert admitted(event, branches=("main", "side", "other"), clearances=("pii", "restricted"))
    assert not admitted(event, branches=None, clearances=())  # not cleared for the Attribute
    assert not admitted(event, branches=("main",), clearances=("pii",))  # source not read
    assert admitted({"x": 1}, branches=("main",), clearances=())  # no audience: as before
    for malformed in ("all", {"branches": "main", "tags": []}, {"branches": [], "tags": "pii"}):
        assert not admitted({AUDIENCE_KEY: malformed}, branches=None, clearances=("pii",))
    assert without_audience(event) == {"x": 1}


def test_proposal_payloads_hold_ids_and_branches_only() -> None:
    at = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
    found = proposal_payload(
        "supply",
        proposal_id="prp_1",
        source="side",
        target="main",
        origin="manual",
        status="merged",
        author="usr_a",
        at=at,
        merged_revision=b"\x01\x02",
        changed=("title",),
    )
    assert without_audience(found) == {
        "domain": "supply",
        "branch": "main",
        "source_branch": "side",
        "proposal": "prp_1",
        "origin": "manual",
        "status": "merged",
        "author": "usr_a",
        "at": "2026-11-16T09:00:00Z",
        "revision": "0102",
        "changed": ["title"],
    }
    assert found[AUDIENCE_KEY] == {"branches": ["main", "side"], "tags": []}


def test_events_past_the_cap_are_counted_in_one_more_event() -> None:
    opened = [ConflictChange(CONFLICT_OPENED, conflict(f"o{i}")) for i in range(5)]
    resolved = [ConflictChange(CONFLICT_RESOLVED, conflict("r0", resolved=True), "resolution")]
    split = capped([*opened, *resolved], 3)
    assert [(kind, len(written), len(counted)) for kind, written, counted in split] == [
        (CONFLICT_OPENED, 3, 5),
        (CONFLICT_RESOLVED, 1, 0),  # within the cap: no summary
    ]
    hidden = [*opened, ConflictChange(CONFLICT_OPENED, conflict("o9", attribute_id=NOTES))]
    summary = truncation_payload(
        "supply",
        hidden,
        SchemaView.of([schema()], None),
        proposal_id="prp_1",
        target="main",
        source="side",
        attempt_id="mga_1",
    )
    assert without_audience(summary) == {
        "domain": "supply",
        "branch": "main",
        "source_branch": "side",
        "proposal": "prp_1",
        "attempt": "mga_1",
        "count": 6,
        "truncated": True,
    }
    # It counts a Conflict on a PII Attribute: only a webhook cleared for PII receives it.
    assert summary[AUDIENCE_KEY] == {"branches": ["main", "side"], "tags": ["pii"]}
