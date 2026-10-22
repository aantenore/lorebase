"""The ``authority`` and ``newest_wins`` merge policies (design 6.10)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from lorebase_core.merge import (
    ChangeStamp,
    ConflictKind,
    LineageLoader,
    MergePolicy,
    Resolution,
    ResolutionChoice,
    Side,
    origin_labels,
)
from lorebase_core.model import (
    ExtractionOrigin,
    OperationOrigin,
    ProvenanceEntry,
    SourceSyncOrigin,
)
from lorebase_core.tree import kind_partition

from .support import (
    COUNTRY,
    NAME,
    SUPPLIER,
    Edit,
    Fork,
    attempt,
    base_schema,
    fork,
    ident,
    merged,
    replace_attribute,
    supplier,
)

ERP = ident("src", 1)
CRM = ident("src", 2)
PARTITION = kind_partition(SUPPLIER)


def sync(n: int, source: str, at: str) -> ProvenanceEntry:
    return ProvenanceEntry(
        id=ident("prv", n),
        origin=SourceSyncOrigin(source_id=source, run_id=f"run-{n}"),
        actor="scribe",
        at=at,
    )


def edit(n: int, principal: str, at: str) -> ProvenanceEntry:
    return ProvenanceEntry(
        id=ident("prv", n),
        origin=OperationOrigin(
            invocation_id=f"inv-{n}", operation="update_supplier", principal=principal
        ),
        actor=principal,
        at=at,
    )


T1, T2 = "2026-03-01T10:00:00Z", "2026-03-02T10:00:00Z"


def _conflicting(schema_authority: str | None = None) -> Fork:
    """``S-1`` renamed and moved to another country on both sides (two ``DV1``)."""
    schema = base_schema()
    if schema_authority is not None:
        schema = replace_attribute(schema, SUPPLIER, NAME, authority=schema_authority)
    return fork(
        Edit().upsert(supplier("S-1", "Ours", vat="IT001", country="FR", rating="3")),
        Edit().upsert(supplier("S-1", "Theirs", vat="IT001", country="DE", rating="3")),
        schema=schema,
    )


def lineage(
    scenario: Fork,
    ours: Mapping[str, ProvenanceEntry],
    theirs: Mapping[str, ProvenanceEntry],
) -> LineageLoader:
    return by_revision({scenario.ours: ours, scenario.theirs: theirs})


def by_revision(table: Mapping[bytes, Mapping[str, ProvenanceEntry]]) -> LineageLoader:
    """Lineage of ``S-1`` per Revision."""
    stamps = {r: {a: ChangeStamp.of(e) for a, e in entries.items()} for r, entries in table.items()}

    def load(revision: bytes, partition: str, key: str) -> Mapping[str, ChangeStamp]:
        if partition != PARTITION or key != "S-1":
            return {}
        return stamps.get(revision, {})

    return load


def _both(
    ours: ProvenanceEntry, theirs: ProvenanceEntry
) -> tuple[dict[str, ProvenanceEntry], dict[str, ProvenanceEntry]]:
    return {NAME: ours, COUNTRY: ours}, {NAME: theirs, COUNTRY: theirs}


def _values(scenario: Fork, root: bytes) -> dict[str, object]:
    values = scenario.repo.values(root, SUPPLIER, "S-1")
    assert values is not None
    return dict(values)


# authority -------------------------------------------------------------------------------


def test_higher_ranked_source_wins_every_attribute() -> None:
    scenario = _conflicting()
    loader = lineage(scenario, *_both(edit(1, "alice", T2), sync(2, ERP, T1)))
    policy = MergePolicy(authority=(f"source:{ERP}", "principal:alice"))
    result = merged(scenario.merge(policy=policy, lineage=loader))
    values = _values(scenario, result.root)
    assert (values[NAME], values[COUNTRY]) == ("Theirs", "DE")
    assert {a.resolver for a in result.auto_resolutions} == {"policy:authority"}
    assert {a.choice for a in result.auto_resolutions} == {ResolutionChoice.THEIRS}
    details = result.auto_resolutions[0].details
    assert details["theirs_origin"] == f"source:{ERP}"
    assert details["ours_origin"] == "principal:alice"
    assert details["theirs_provenance"] == ident("prv", 2)


def test_authority_picks_ours_when_ours_ranks_higher() -> None:
    scenario = _conflicting()
    loader = lineage(scenario, *_both(sync(1, ERP, T1), sync(2, CRM, T2)))
    policy = MergePolicy(authority=(f"source:{ERP}", f"source:{CRM}"))
    result = merged(scenario.merge(policy=policy, lineage=loader))
    assert _values(scenario, result.root)[NAME] == "Ours"


def test_a_listed_origin_outranks_an_unlisted_one() -> None:
    scenario = _conflicting()
    loader = lineage(scenario, *_both(edit(1, "bob", T2), sync(2, ERP, T1)))
    result = merged(
        scenario.merge(policy=MergePolicy(authority=(f"source:{ERP}",)), lineage=loader)
    )
    assert _values(scenario, result.root)[NAME] == "Theirs"


@pytest.mark.parametrize(
    ("ours", "theirs"),
    [
        (edit(1, "bob", T1), edit(2, "carol", T2)),  # neither listed
        (sync(1, ERP, T1), sync(2, ERP, T2)),  # same rank
    ],
)
def test_authority_leaves_equal_or_unknown_ranks_to_a_person(
    ours: ProvenanceEntry, theirs: ProvenanceEntry
) -> None:
    scenario = _conflicting()
    loader = lineage(scenario, *_both(ours, theirs))
    result = attempt(
        scenario.merge(policy=MergePolicy(authority=(f"source:{ERP}",)), lineage=loader)
    )
    assert [c.kind for c in result.conflicts] == [ConflictKind.ATTRIBUTE_VALUE] * 2
    assert result.auto_resolutions == ()


def test_authority_without_lineage_decides_nothing() -> None:
    scenario = _conflicting()
    policy = MergePolicy(authority=(f"source:{ERP}",))
    assert len(attempt(scenario.merge(policy=policy)).conflicts) == 2
    one_sided = lineage(scenario, {}, {NAME: sync(2, ERP, T1)})
    assert len(attempt(scenario.merge(policy=policy, lineage=one_sided)).conflicts) == 2


def test_authority_per_attribute() -> None:
    scenario = _conflicting()
    loader = lineage(scenario, *_both(edit(1, "alice", T2), sync(2, ERP, T1)))
    policy = MergePolicy(authority_attributes={NAME: (f"source:{ERP}",)})
    result = attempt(scenario.merge(policy=policy, lineage=loader))
    assert [c.attribute for c in result.conflicts] == [COUNTRY]
    assert [(a.attribute, a.resolver) for a in result.auto_resolutions] == [
        (NAME, "policy:authority")
    ]


def test_schema_authority_ranks_the_attribute_source_first() -> None:
    scenario = _conflicting(schema_authority=ERP)
    loader = lineage(scenario, *_both(sync(1, CRM, T2), sync(2, ERP, T1)))
    without = attempt(scenario.merge(lineage=loader))
    assert len(without.conflicts) == 2
    policy = MergePolicy(schema_authority=True, authority_attributes={NAME: (f"source:{CRM}",)})
    result = attempt(scenario.merge(policy=policy, lineage=loader))
    assert [c.attribute for c in result.conflicts] == [COUNTRY]
    assert result.auto_resolutions[0].choice is ResolutionChoice.THEIRS


# newest_wins ------------------------------------------------------------------------------


def test_newest_change_wins() -> None:
    scenario = _conflicting()
    loader = lineage(scenario, *_both(edit(1, "alice", T2), sync(2, ERP, T1)))
    result = merged(scenario.merge(policy=MergePolicy(newest_wins=True), lineage=loader))
    values = _values(scenario, result.root)
    assert (values[NAME], values[COUNTRY]) == ("Ours", "FR")
    assert {a.resolver for a in result.auto_resolutions} == {"policy:newest_wins"}
    assert result.auto_resolutions[0].details["ours_at"] == "2026-03-02T10:00:00+00:00"


def test_newest_wins_compares_instants_not_text() -> None:
    scenario = _conflicting()
    # 11:00+02:00 is 09:00Z, earlier than 10:00Z.
    ours = edit(1, "alice", "2026-03-01T11:00:00+02:00")
    loader = lineage(scenario, *_both(ours, sync(2, ERP, T1)))
    result = merged(scenario.merge(policy=MergePolicy(newest_wins=True), lineage=loader))
    assert _values(scenario, result.root)[NAME] == "Theirs"


def test_equal_times_are_decided_by_the_greater_provenance_id() -> None:
    scenario = _conflicting()
    loader = lineage(scenario, *_both(edit(7, "alice", T1), sync(3, ERP, T1)))
    policy = MergePolicy(newest_wins=True)
    result = merged(scenario.merge(policy=policy, lineage=loader))
    assert _values(scenario, result.root)[NAME] == "Ours"
    assert result.auto_resolutions[0].details["tie_break"] == "provenance_id"
    swapped = lineage(scenario, *_both(edit(3, "alice", T1), sync(7, ERP, T1)))
    again = merged(scenario.merge(policy=policy, lineage=swapped))
    assert _values(scenario, again.root)[NAME] == "Theirs"


def test_same_entry_on_both_sides_is_left_to_a_person() -> None:
    scenario = _conflicting()
    same = sync(1, ERP, T1)
    loader = lineage(scenario, *_both(same, same))
    result = attempt(scenario.merge(policy=MergePolicy(newest_wins=True), lineage=loader))
    assert len(result.conflicts) == 2


def test_newest_wins_for_listed_attributes_only() -> None:
    scenario = _conflicting()
    loader = lineage(scenario, *_both(edit(1, "alice", T2), sync(2, ERP, T1)))
    policy = MergePolicy(newest_wins_attributes=frozenset({COUNTRY}))
    result = attempt(scenario.merge(policy=policy, lineage=loader))
    assert [c.attribute for c in result.conflicts] == [NAME]


def test_newest_wins_is_the_same_whichever_side_is_ours() -> None:
    scenario = _conflicting()
    load = lineage(scenario, *_both(edit(1, "alice", T1), sync(2, ERP, T2)))
    policy = MergePolicy(newest_wins=True)
    forward = merged(scenario.merge(policy=policy, lineage=load))
    backward = merged(scenario.reverse(policy=policy, lineage=load))
    assert _values(scenario, forward.root) == _values(scenario, backward.root)


# Rule order and scope ------------------------------------------------------------------------


def test_rules_run_from_most_to_least_specific() -> None:
    policy = MergePolicy(
        prefer=Side.OURS,
        prefer_elements={SUPPLIER: Side.THEIRS},
        prefer_attributes={NAME: Side.OURS},
        authority=("origin:source_sync",),
        authority_attributes={NAME: ("principal:alice",)},
        newest_wins=True,
        newest_wins_attributes=frozenset({NAME}),
    )
    names = [r.name.value for r in policy.rules(SUPPLIER, NAME)]
    assert names == [
        "prefer",
        "authority",
        "newest_wins",
        "prefer",
        "authority",
        "newest_wins",
        "prefer",
    ]
    assert [r.name.value for r in policy.rules(SUPPLIER, None)] == ["prefer", "prefer"]


def test_an_undecided_rule_falls_through_to_the_next() -> None:
    scenario = _conflicting()
    loader = lineage(scenario, *_both(edit(1, "bob", T2), edit(2, "carol", T1)))
    policy = MergePolicy(authority=(f"source:{ERP}",), newest_wins=True, prefer=Side.THEIRS)
    result = merged(scenario.merge(policy=policy, lineage=loader))
    assert _values(scenario, result.root)[NAME] == "Ours"
    assert {a.resolver for a in result.auto_resolutions} == {"policy:newest_wins"}


def test_prefer_on_the_attribute_comes_before_authority() -> None:
    scenario = _conflicting()
    loader = lineage(scenario, *_both(edit(1, "alice", T2), sync(2, ERP, T1)))
    policy = MergePolicy(prefer_attributes={NAME: Side.OURS}, authority=(f"source:{ERP}",))
    result = merged(scenario.merge(policy=policy, lineage=loader))
    values = _values(scenario, result.root)
    assert (values[NAME], values[COUNTRY]) == ("Ours", "DE")
    by_attribute = {a.attribute: a.resolver for a in result.auto_resolutions}
    assert by_attribute == {NAME: "policy:prefer", COUNTRY: "policy:authority"}


def test_a_given_resolution_overrides_the_policy() -> None:
    scenario = _conflicting()
    loader = lineage(scenario, *_both(edit(1, "alice", T2), sync(2, ERP, T1)))
    plain = attempt(scenario.merge(lineage=loader))
    name = next(c for c in plain.conflicts if c.attribute == NAME)
    given = Resolution(fingerprint=name.fingerprint, choice=ResolutionChoice.THEIRS, resolver="u")
    result = merged(
        scenario.merge(policy=MergePolicy(newest_wins=True), lineage=loader, resolutions=(given,))
    )
    values = _values(scenario, result.root)
    assert (values[NAME], values[COUNTRY]) == ("Theirs", "FR")
    assert [a.attribute for a in result.auto_resolutions] == [COUNTRY]


def test_delete_modify_is_not_decided_by_lineage_policies() -> None:
    scenario = fork(
        Edit().remove(supplier("S-1")),
        Edit().upsert(supplier("S-1", "Theirs", vat="IT001", country="IT", rating="3")),
    )
    loader = lineage(scenario, {}, {NAME: sync(2, ERP, T1)})
    policy = MergePolicy(newest_wins=True, authority=(f"source:{ERP}",))
    result = attempt(scenario.merge(policy=policy, lineage=loader))
    assert [c.kind for c in result.conflicts] == [ConflictKind.DELETE_MODIFY]


# Labels and validation ---------------------------------------------------------------------------


def test_origin_labels() -> None:
    assert origin_labels(sync(1, ERP, T1)) == {
        "origin:source_sync",
        "actor:scribe",
        f"source:{ERP}",
    }
    assert origin_labels(edit(1, "alice", T1)) == {
        "origin:operation",
        "actor:alice",
        "principal:alice",
        "operation:update_supplier",
    }
    extraction = ProvenanceEntry(
        id=ident("prv", 9),
        origin=ExtractionOrigin(
            job_id="j",
            document_id="d",
            span_start=0,
            span_end=1,
            model_id="m",
            prompt_version="1",
        ),
        actor="scribe",
        at=T1,
    )
    assert origin_labels(extraction) == {"origin:extraction", "actor:scribe"}


def test_stamps_of_stored_entries() -> None:
    at = datetime(2026, 3, 1, 10)  # naive times are UTC
    stored = ChangeStamp.from_stored(
        ident("prv", 1),
        "source_sync",
        {"type": "source_sync", "source_id": ERP, "run_id": "r"},
        "scribe",
        at,
    )
    assert stored.labels == origin_labels(sync(1, ERP, T1))
    assert stored.at == at.replace(tzinfo=UTC)
    commit = ChangeStamp.from_stored(
        ident("prv", 2), "import", {"type": "import", "ref": None}, "usr_loader", at
    )
    assert commit.labels == {"origin:import", "actor:usr_loader"}


@pytest.mark.parametrize(
    "options",
    [
        {"authority": ("erp",)},
        {"authority": ("vendor:x",)},
        {"authority": ("source:",)},
        {"authority": ("source:a", "source:a")},
        {"authority_attributes": {NAME: ()}},
        {"authority_attributes": {NAME: ("nope",)}},
    ],
)
def test_invalid_rankings_are_rejected(options: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MergePolicy.model_validate(options)


def test_policy_round_trips_through_json() -> None:
    policy = MergePolicy(
        authority=(f"source:{ERP}", "principal:alice"),
        authority_attributes={NAME: ("actor:bob",)},
        schema_authority=True,
        newest_wins_attributes=frozenset({COUNTRY}),
    )
    dumped = policy.model_dump(mode="json", exclude_defaults=True)
    assert MergePolicy.model_validate(dumped) == policy
