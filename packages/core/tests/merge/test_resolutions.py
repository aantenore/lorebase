"""Resolving Conflicts: choices per kind, validation, re-checks and stale attempts."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import JsonValue

from lorebase_core.merge import (
    ConflictKind,
    MergeAttempt,
    MergedTree,
    Resolution,
    ResolutionChoice,
    ResolutionError,
    UnresolvedConflictsError,
    apply_resolutions,
)
from lorebase_core.model import Cardinality

from . import test_conflict_kinds as cases
from .support import (
    CODE,
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
    fork,
    merged,
    part,
    supplied_by,
    supplier,
)

K = ConflictKind
C = ResolutionChoice


def resolve_only(
    scenario: Fork, choice: ResolutionChoice, **extra: Any
) -> tuple[MergeAttempt, MergedTree]:
    """Resolve the single Conflict of a scenario; the result must be clean."""
    first = attempt(scenario.merge())
    assert len(first.conflicts) == 1
    resolution = Resolution(fingerprint=first.conflicts[0].fingerprint, choice=choice, **extra)
    return first, merged(apply_resolutions(first, [resolution]))


def supplier_values(scenario: Fork, result: MergedTree, key: str) -> dict[str, JsonValue]:
    values = scenario.repo.values(result.root, SUPPLIER, key)
    assert values is not None
    return values


@pytest.mark.parametrize(
    ("choice", "value", "expected"),
    [
        (C.OURS, None, "Ours"),
        (C.THEIRS, None, "Theirs"),
        (C.BASE, None, "Supplier S-1"),
        (C.CUSTOM, "Agreed", "Agreed"),
    ],
)
def test_attribute_value_choices(
    choice: ResolutionChoice, value: str | None, expected: str
) -> None:
    scenario = cases._dv1()
    _, result = resolve_only(scenario, choice, value=value)
    assert supplier_values(scenario, result, "S-1")[NAME] == expected
    assert result.resolutions[0].choice is choice


def test_delete_modify_keeps_deleted_or_modified() -> None:
    scenario = cases._dv2()
    _, deleted = resolve_only(scenario, C.OURS)
    assert scenario.repo.values(deleted.root, PART, "P-3") is None
    _, kept = resolve_only(scenario, C.THEIRS)
    assert scenario.repo.values(kept.root, PART, "P-3") is not None


def test_a_kept_record_keeps_the_links_its_deletion_removed() -> None:
    # Theirs deletes P-1 with its Link, ours modifies P-1.
    scenario = fork(
        Edit().upsert(part("P-1", "Renamed")),
        Edit().remove(part("P-1"), supplied_by("P-1", "S-1")),
    )
    _, kept = resolve_only(scenario, C.OURS)
    assert scenario.repo.values(kept.root, PART, "P-1") is not None
    assert scenario.repo.has_link(kept.root, SUPPLIED_BY, "P-1", "S-1")
    _, deleted = resolve_only(scenario, C.THEIRS)
    assert scenario.repo.values(deleted.root, PART, "P-1") is None
    assert not scenario.repo.has_link(deleted.root, SUPPLIED_BY, "P-1", "S-1")


def test_links_into_a_kept_record_and_from_either_side_are_kept() -> None:
    # A Link pointing at the Record (removed by `on_delete`) is kept as well.
    into = fork(
        Edit().upsert(supplier("S-1", "Renamed", vat="IT001", country="IT", rating="3")),
        Edit().remove(supplier("S-1"), supplied_by("P-1", "S-1")),
    )
    _, kept = resolve_only(into, C.OURS)
    assert into.repo.has_link(kept.root, SUPPLIED_BY, "P-1", "S-1")
    # Ours deleted, theirs modified, the modified version wins: ours' Link comes back.
    reverse = fork(
        Edit().remove(part("P-1"), supplied_by("P-1", "S-1")),
        Edit().upsert(part("P-1", "Renamed")),
    )
    _, restored = resolve_only(reverse, C.THEIRS)
    assert reverse.repo.has_link(restored.root, SUPPLIED_BY, "P-1", "S-1")


def test_a_link_the_keeping_side_removed_stays_removed() -> None:
    scenario = fork(
        Edit().upsert(part("P-1", "Renamed")).remove(supplied_by("P-1", "S-1")),
        Edit().remove(part("P-1"), supplied_by("P-1", "S-1")),
    )
    _, kept = resolve_only(scenario, C.OURS)
    assert scenario.repo.values(kept.root, PART, "P-1") is not None
    assert not scenario.repo.has_link(kept.root, SUPPLIED_BY, "P-1", "S-1")


def test_schema_property_custom_value() -> None:
    scenario = cases._sc1()
    _, result = resolve_only(scenario, C.CUSTOM, value="agreed")
    schema = scenario.repo.schema(result.root)
    assert schema.kinds[SUPPLIER].attributes[COUNTRY].description == "agreed"


def test_naming_collision_resolved_by_renaming() -> None:
    scenario = cases._sc3()
    _, result = resolve_only(scenario, C.CUSTOM, value="Distributor")
    schema = scenario.repo.schema(result.root)
    assert schema.kinds[cases.VENDOR_THEIRS].api_name == "Distributor"
    assert schema.kinds[cases.VENDOR_OURS].api_name == "Vendor"


def test_orphaned_data_drop_keeps_the_removal() -> None:
    scenario = cases._xs1()
    _, result = resolve_only(scenario, C.DROP)
    assert PART not in scenario.repo.schema(result.root).kinds
    assert scenario.repo.values(result.root, PART, "P-1") is None


def test_orphaned_data_restore_brings_back_kind_and_data() -> None:
    scenario = cases._xs1()
    _, result = resolve_only(scenario, C.RESTORE)
    assert PART in scenario.repo.schema(result.root).kinds
    assert scenario.repo.values(result.root, PART, "P-1") is not None
    assert scenario.repo.values(result.root, PART, "P-2") is not None


def test_orphaned_value_restore_brings_back_the_attribute() -> None:
    scenario = cases._xs2()
    _, result = resolve_only(scenario, C.RESTORE)
    assert RATING in scenario.repo.schema(result.root).kinds[SUPPLIER].attributes
    assert supplier_values(scenario, result, "S-2")[RATING] == "5"
    assert supplier_values(scenario, result, "S-1")[RATING] == "3"


def test_orphaned_value_drop_removes_the_values() -> None:
    scenario = cases._xs2()
    _, result = resolve_only(scenario, C.DROP)
    assert RATING not in supplier_values(scenario, result, "S-2")


def test_type_coercion_custom_value_and_revert() -> None:
    scenario = cases._xs3()
    _, custom = resolve_only(scenario, C.CUSTOM, values={"S-2": "DE"})
    assert supplier_values(scenario, custom, "S-2")[COUNTRY] == "DE"
    _, reverted = resolve_only(scenario, C.RESTORE)
    schema = scenario.repo.schema(reverted.root)
    assert schema.kinds[SUPPLIER].attributes[COUNTRY].type.name == "string"
    assert supplier_values(scenario, reverted, "S-2")[COUNTRY] == "FR"


def test_type_coercion_drop_deletes_the_records() -> None:
    scenario = cases._xs3()
    _, result = resolve_only(scenario, C.DROP)
    assert scenario.repo.values(result.root, SUPPLIER, "S-2") is None


def test_missing_value_custom_and_relax() -> None:
    scenario = cases._xs4()
    _, custom = resolve_only(scenario, C.CUSTOM, value="DE")
    assert supplier_values(scenario, custom, "S-9")[COUNTRY] == "DE"
    _, relaxed = resolve_only(scenario, C.RELAX)
    schema = scenario.repo.schema(relaxed.root)
    assert not schema.kinds[SUPPLIER].attributes[COUNTRY].required


def test_constraint_relax_restores_base_constraints() -> None:
    scenario = cases._xs5()
    _, result = resolve_only(scenario, C.RELAX)
    attribute = scenario.repo.schema(result.root).kinds[SUPPLIER].attributes[VAT]
    assert attribute.constraints.pattern is None


def test_cardinality_custom_keeps_chosen_links() -> None:
    scenario = cases._xs6()
    _, result = resolve_only(scenario, C.CUSTOM, value=[["P-3", "S-2"]])
    assert scenario.repo.has_link(result.root, SUPPLIED_BY, "P-3", "S-2")
    assert not scenario.repo.has_link(result.root, SUPPLIED_BY, "P-3", "S-1")


def test_cardinality_relax() -> None:
    scenario = cases._xs6()
    _, result = resolve_only(scenario, C.RELAX)
    relation = scenario.repo.schema(result.root).relations[SUPPLIED_BY]
    assert relation.cardinality is Cardinality.MANY_TO_MANY


def test_required_relation_custom_link_drop_and_relax() -> None:
    scenario = cases._xs7()
    _, linked = resolve_only(scenario, C.CUSTOM, values={"P-9": "S-1"})
    assert scenario.repo.has_link(linked.root, SUPPLIED_BY, "P-9", "S-1")
    _, dropped = resolve_only(scenario, C.DROP)
    assert scenario.repo.values(dropped.root, PART, "P-9") is None
    _, relaxed = resolve_only(scenario, C.RELAX)
    assert not scenario.repo.schema(relaxed.root).relations[SUPPLIED_BY].required_from


def test_uniqueness_custom_value_and_relax() -> None:
    scenario = cases._xs8()
    _, custom = resolve_only(scenario, C.CUSTOM, values={"S-2": "IT888"})
    assert supplier_values(scenario, custom, "S-1")[VAT] == "IT777"
    assert supplier_values(scenario, custom, "S-2")[VAT] == "IT888"
    _, relaxed = resolve_only(scenario, C.RELAX)
    assert not scenario.repo.schema(relaxed.root).kinds[SUPPLIER].attributes[VAT].unique


def test_dangling_link_drop_and_restore() -> None:
    scenario = cases._xs9()
    _, dropped = resolve_only(scenario, C.DROP)
    assert not scenario.repo.has_link(dropped.root, SUPPLIED_BY, "P-3", "S-3")
    assert scenario.repo.values(dropped.root, SUPPLIER, "S-3") is None
    _, restored = resolve_only(scenario, C.RESTORE)
    assert scenario.repo.has_link(restored.root, SUPPLIED_BY, "P-3", "S-3")
    assert scenario.repo.values(restored.root, SUPPLIER, "S-3") is not None


def test_trait_conformance_relax_removes_the_trait() -> None:
    scenario = cases._xs10()
    _, result = resolve_only(scenario, C.RELAX)
    assert scenario.repo.schema(result.root).kinds[SUPPLIER].traits == ()


def test_trait_conformance_custom_mapping() -> None:
    scenario = cases._xs10()
    first = attempt(scenario.merge())
    resolution = Resolution(
        fingerprint=first.conflicts[0].fingerprint, choice=C.CUSTOM, value={LABEL: CODE}
    )
    result = merged(apply_resolutions(first, [resolution]))
    traits = scenario.repo.schema(result.root).kinds[SUPPLIER].traits
    assert traits[0].trait_id == NAMED
    assert traits[0].attribute_map == {LABEL: CODE}


def test_broken_reference_restore() -> None:
    scenario = cases._xs11()
    _, result = resolve_only(scenario, C.RESTORE)
    kind = scenario.repo.schema(result.root).kinds[SUPPLIER]
    assert kind.title_attribute == COUNTRY
    assert COUNTRY in kind.attributes


# Validation -----------------------------------------------------------------------------


def test_every_conflict_needs_a_resolution() -> None:
    scenario = cases._dv1()
    first = attempt(scenario.merge())
    with pytest.raises(UnresolvedConflictsError) as caught:
        apply_resolutions(first, [])
    assert caught.value.fingerprints == (first.conflicts[0].fingerprint,)


def test_unknown_fingerprint_is_rejected() -> None:
    first = attempt(cases._dv1().merge())
    with pytest.raises(ResolutionError):
        apply_resolutions(first, [Resolution(fingerprint="0" * 64, choice=C.OURS)])


def test_choice_must_fit_the_kind() -> None:
    first = attempt(cases._dv1().merge())
    wrong = Resolution(fingerprint=first.conflicts[0].fingerprint, choice=C.RELAX)
    with pytest.raises(ResolutionError):
        apply_resolutions(first, [wrong])


def test_grouped_custom_values_cover_every_member() -> None:
    from .support import base_schema, fork, replace_attribute

    required = replace_attribute(base_schema(), SUPPLIER, COUNTRY, required=True)
    scenario = fork(
        Edit(schema=required).upsert(supplier("S-3", vat="IT003", country="IT")),
        Edit().upsert(supplier("S-8"), supplier("S-9")),
    )
    first = attempt(scenario.merge())
    assert first.conflicts[0].count == 2
    partial = Resolution(
        fingerprint=first.conflicts[0].fingerprint, choice=C.CUSTOM, values={"S-9": "DE"}
    )
    with pytest.raises(ResolutionError):
        apply_resolutions(first, [partial])


def test_only_custom_resolutions_carry_values() -> None:
    with pytest.raises(ValueError, match="custom"):
        Resolution(fingerprint="0" * 64, choice=C.OURS, value="x")


def test_resolution_can_surface_a_new_conflict() -> None:
    scenario = fork_with_duplicate_vat()
    first = attempt(scenario.merge())
    assert [c.kind for c in first.conflicts] == [K.ATTRIBUTE_VALUE]
    duplicate = Resolution(
        fingerprint=first.conflicts[0].fingerprint, choice=C.CUSTOM, value="IT002"
    )
    second = attempt(apply_resolutions(first, [duplicate]))
    assert [c.kind for c in second.conflicts] == [K.UNIQUENESS]
    fix = Resolution(
        fingerprint=second.conflicts[0].fingerprint,
        choice=C.CUSTOM,
        values={"S-1": "IT009", "S-2": "IT002"},
    )
    result = merged(apply_resolutions(second, [fix]))
    assert supplier_values(scenario, result, "S-1")[VAT] == "IT009"


def test_unfixed_check_conflict_is_reported_again() -> None:
    scenario = cases._xs5()
    first = attempt(scenario.merge())
    still_bad = Resolution(
        fingerprint=first.conflicts[0].fingerprint, choice=C.CUSTOM, value="FR123"
    )
    second = attempt(apply_resolutions(first, [still_bad]))
    assert [c.kind for c in second.conflicts] == [K.CONSTRAINT]


def fork_with_duplicate_vat() -> Fork:
    from .support import fork

    return fork(
        Edit().upsert(supplier("S-1", vat="IT100", country="IT", rating="3")),
        Edit().upsert(supplier("S-1", vat="IT200", country="IT", rating="3")),
    )


def test_stale_attempt_reapplies_matching_resolutions() -> None:
    scenario = cases._dv1()
    first = attempt(scenario.merge())
    resolution = Resolution(fingerprint=first.conflicts[0].fingerprint, choice=C.THEIRS)
    # The target moves on before the resolution is applied.
    moved = scenario.repo.commit("ours-2", Edit().upsert(supplier("S-7")), scenario.ours)
    again = scenario.repo.merge(moved, scenario.theirs, resolutions=(resolution,))
    result = merged(again)
    assert supplier_values(scenario, result, "S-1")[NAME] == "Theirs"
    assert scenario.repo.values(result.root, SUPPLIER, "S-7") is not None


def test_stale_attempt_shows_changed_conflicts_as_unresolved() -> None:
    scenario = cases._dv1()
    first = attempt(scenario.merge())
    resolution = Resolution(fingerprint=first.conflicts[0].fingerprint, choice=C.THEIRS)
    moved = scenario.repo.commit(
        "ours-2",
        Edit().upsert(supplier("S-1", "Ours again", vat="IT001", country="IT", rating="3")),
        scenario.ours,
    )
    again = attempt(scenario.repo.merge(moved, scenario.theirs, resolutions=(resolution,)))
    assert [c.kind for c in again.conflicts] == [K.ATTRIBUTE_VALUE]
    assert again.conflicts[0].fingerprint != resolution.fingerprint
