"""Pure functions of reconciliation (design 10.6): source identities, claims, fingerprints,
period classification, claim folding, normalization and options."""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from lorebase_core.model import (
    ExtractionOrigin,
    ImportOrigin,
    MergeResolutionOrigin,
    OperationOrigin,
    ProvenanceEntry,
    ReconciliationOrigin,
    SchemaMigrationOrigin,
    SourceSyncOrigin,
)
from lorebase_core.reconcile import (
    CandidateDraft,
    CandidateType,
    Claim,
    Comparison,
    NormalizeOptions,
    ReconcileOptions,
    Signal,
    Stage,
    StaleOptions,
    UnitTable,
    UnitTableError,
    candidate_fingerprint,
    classify,
    compare,
    disagree,
    merge_claims,
    normalize_claims,
    parse_quantity,
    source_identity,
    value_hash,
)

from ..model.supply import ident

SOURCE = ident("src", 1)
ATTR = ident("att", 1)
KIND = ident("knd", 1)
UNITS = UnitTable.load()
NORMALIZE = NormalizeOptions()


def claim(
    claim_id: str,
    value: Any,
    source: str | None = "source:a",
    at: str = "2026-10-01T00:00:00Z",
    **period: str | None,
) -> Claim:
    return Claim(
        claim_id=claim_id,
        value=value,
        source=source,
        provenance_id=claim_id,
        at=at,
        **period,
    )


# Source identity ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        (SourceSyncOrigin(source_id=SOURCE, run_id="r1"), f"source:{SOURCE}"),
        (
            ExtractionOrigin(
                job_id="j",
                document_id="doc_1",
                span_start=0,
                span_end=3,
                model_id="m",
                prompt_version="v1",
            ),
            "document:doc_1",
        ),
        (
            OperationOrigin(invocation_id="inv", operation="update_supplier", principal="usr_1"),
            "principal:usr_1",
        ),
        (ImportOrigin(batch_id="b7"), "import:b7"),
    ],
)
def test_source_identity_follows_the_origin(origin: Any, expected: str) -> None:
    assert source_identity(origin) == expected
    assert source_identity(origin, chosen="source:other") == expected


def test_resolutions_carry_the_identity_of_the_chosen_value() -> None:
    merged = MergeResolutionOrigin(proposal_id="p", conflict_id="c", resolver="usr_1")
    assert source_identity(merged) is None
    assert source_identity(merged, chosen="source:a") == "source:a"
    cast = SchemaMigrationOrigin(attribute_id=ATTR)
    assert source_identity(cast, chosen="import:b1") == "import:b1"
    picked = ReconciliationOrigin(
        candidate_id="rcn_1", claim_id="prv_1", resolver="policy:authority", source="source:a"
    )
    assert source_identity(picked, chosen="source:b") == "source:a"
    custom = ReconciliationOrigin(candidate_id="rcn_1", resolver="principal:usr_9")
    assert source_identity(custom) == "principal:usr_9"
    by_policy = ReconciliationOrigin(candidate_id="rcn_1", resolver="policy:newest_wins")
    assert source_identity(by_policy) is None
    assert source_identity(by_policy, chosen="source:b") == "source:b"


# Claims -------------------------------------------------------------------------------------


def test_claim_of_provenance_takes_identity_period_and_time() -> None:
    entry = ProvenanceEntry(
        id=ident("prv", 1),
        origin=SourceSyncOrigin(source_id=SOURCE, run_id="r"),
        actor="svc_scribe",
        at="2026-10-01T10:00:00Z",
        valid_from="2026-01-01T00:00:00Z",
    )
    found = Claim.of_provenance(entry, "Milan")
    assert found.claim_id == entry.id == found.provenance_id
    assert found.source == f"source:{SOURCE}"
    assert found.valid_from == "2026-01-01T00:00:00.000000Z"
    assert found.valid_to is None
    assert found.value_hash == value_hash("Milan")
    assert found.has_period
    assert found.document()["value"] == "Milan"
    assert "proposal_item" not in found.document()


def test_claim_of_proposal_item() -> None:
    found = Claim.of_proposal_item(
        "prp_1",
        "itm_2",
        "Turin",
        source="document:doc_1",
        at="2026-10-02T00:00:00Z",
        evidence={"document_id": "doc_1", "quote": "based in Turin"},
    )
    assert found.claim_id == "prp_1/itm_2"
    assert found.proposal_item is not None
    assert found.proposal_item.item_id == "itm_2"
    assert not found.has_period


def test_claims_are_checked() -> None:
    with pytest.raises(ValidationError, match="value_hash"):
        Claim(claim_id="c", value=1, value_hash="00", provenance_id="c", at="2026-10-01T00:00:00Z")
    with pytest.raises(ValidationError, match="exactly one"):
        Claim(claim_id="c", value=1, at="2026-10-01T00:00:00Z")
    with pytest.raises(ValidationError, match="valid_to"):
        claim("c", 1, valid_from="2026-02-01T00:00:00Z", valid_to="2026-01-01T00:00:00Z")


def test_value_hash_is_canonical() -> None:
    assert value_hash({"a": 1, "b": [1, 2]}) == value_hash({"b": [1, 2], "a": 1})
    assert value_hash("1") != value_hash(1)


# Fingerprints -------------------------------------------------------------------------------


def test_fingerprint_hashes_values_not_sources() -> None:
    one = CandidateDraft(
        type=CandidateType.CONTRADICTION,
        branch="main",
        kind_id=KIND,
        keys=("S-1",),
        attribute_id=ATTR,
        claims=(claim("c1", "Milan", "source:a"), claim("c2", "Turin", "source:b")),
    )
    repeated = one.model_copy(update={"claims": (*one.claims, claim("c3", "Milan", "source:c"))})
    assert len(one.fingerprint) == 32
    assert repeated.fingerprint == one.fingerprint
    new_value = one.model_copy(update={"claims": (*one.claims, claim("c4", "Rome"))})
    assert new_value.fingerprint != one.fingerprint
    elsewhere = one.model_copy(update={"branch": "dev"})
    assert elsewhere.fingerprint != one.fingerprint
    superseded = one.model_copy(update={"type": CandidateType.SUPERSEDED})
    assert superseded.fingerprint != one.fingerprint


def test_duplicate_keys_are_a_set() -> None:
    first = candidate_fingerprint(CandidateType.DUPLICATE, "main", KIND, ["a", "b"], None, [])
    second = candidate_fingerprint(CandidateType.DUPLICATE, "main", KIND, ["b", "a"], None, [])
    assert first == second
    draft = CandidateDraft(
        type=CandidateType.DUPLICATE, branch="main", kind_id=KIND, keys=("b", "a")
    )
    assert draft.keys == ("a", "b")
    assert draft.fingerprint == first


# Disagreement and classification -----------------------------------------------------------


def test_disagreement_needs_different_values_from_different_sources() -> None:
    assert not disagree([claim("1", "x", "source:a"), claim("2", "x", "source:b")])
    assert not disagree([claim("1", "x", "source:a"), claim("2", "y", "source:a")])
    assert disagree([claim("1", "x", "source:a"), claim("2", "y", "source:b")])
    assert disagree([claim("1", "x", None), claim("2", "y", None)])
    assert not disagree([claim("1", "x")])


def test_without_valid_time_every_disagreement_is_a_contradiction() -> None:
    claims = [
        claim("1", "x", valid_from="2025-01-01T00:00:00Z", valid_to="2026-01-01T00:00:00Z"),
        claim("2", "y", valid_from="2026-01-01T00:00:00Z"),
    ]
    assert classify(claims, valid_time=False) is CandidateType.CONTRADICTION
    assert classify(claims, valid_time=True) is CandidateType.SUPERSEDED


@pytest.mark.parametrize(
    ("first", "second"),
    [
        # overlapping
        (
            {"valid_from": "2025-01-01T00:00:00Z", "valid_to": "2026-06-01T00:00:00Z"},
            {"valid_from": "2026-01-01T00:00:00Z"},
        ),
        # equal
        ({"valid_from": "2026-01-01T00:00:00Z"}, {"valid_from": "2026-01-01T00:00:00Z"}),
        # one claim without a period
        ({"valid_from": "2026-01-01T00:00:00Z"}, {}),
        # open start reaching into the other period
        ({"valid_to": "2026-02-01T00:00:00Z"}, {"valid_from": "2026-01-01T00:00:00Z"}),
    ],
)
def test_overlapping_or_missing_periods_are_contradictions(
    first: dict[str, str], second: dict[str, str]
) -> None:
    claims = [claim("1", "x", **first), claim("2", "y", **second)]
    assert classify(claims, valid_time=True) is CandidateType.CONTRADICTION


def test_equal_values_may_overlap() -> None:
    claims = [
        claim("1", "x", valid_from="2025-01-01T00:00:00Z"),
        claim("2", "x", valid_from="2025-06-01T00:00:00Z", valid_to="2026-01-01T00:00:00Z"),
        claim("3", "y", valid_to="2025-01-01T00:00:00Z"),
    ]
    assert classify(claims, valid_time=True) is CandidateType.SUPERSEDED


# Claim folding ------------------------------------------------------------------------------


def doc(claim_id: str, at: str) -> dict[str, Any]:
    return {"claim_id": claim_id, "at": at, "value": claim_id}


def test_merge_claims_keeps_the_newest_and_counts_the_rest() -> None:
    stored = [doc("a", "2026-01-01"), doc("b", "2026-02-01")]
    incoming = [doc("b", "2026-09-09"), doc("c", "2026-03-01"), doc("d", "2025-01-01")]
    merged = merge_claims(stored, incoming, stored_count=2, max_claims=3)
    assert [c["claim_id"] for c in merged.kept] == ["a", "b", "c"]
    assert merged.kept[1]["at"] == "2026-02-01"  # a stored claim is kept as stored
    assert merged.added == 2
    assert merged.count == 4
    assert merged.dropped == 1
    again = merge_claims(merged.kept, incoming, stored_count=merged.count, max_claims=3)
    assert again.added == 1
    assert again.count == 5


def test_merge_claims_needs_room() -> None:
    with pytest.raises(ValueError, match="positive"):
        merge_claims([], [], stored_count=0, max_claims=0)


# Normalization ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("5 km", "5000 m", Comparison.EQUAL),
        ("5 km", "7 km", Comparison.DIFFERENT),
        ("5 km", "5 kg", Comparison.DIFFERENT),
        ("2 Kilometres", "2000m", Comparison.EQUAL),
        ("1 lb", "453.59237 g", Comparison.EQUAL),
        ("12", "12.0", Comparison.EQUAL),
        ("Acme,  Inc.", "acme inc", Comparison.EQUAL),
        ("\uff21\uff23\uff2d\uff25", "acme", Comparison.EQUAL),  # fullwidth letters
        ("Certified", "not certified", Comparison.NEGATION),
        ("never shipped late", "shipped late", Comparison.NEGATION),
        ("Milan", "Turin", Comparison.DIFFERENT),
        (3, 3.0, Comparison.EQUAL),
        (3, "3", Comparison.DIFFERENT),
        (True, 1, Comparison.DIFFERENT),
    ],
)
def test_compare(first: Any, second: Any, expected: Comparison) -> None:
    assert compare(first, second, options=NORMALIZE, units=UNITS) is expected


def test_numeric_tolerance_is_relative() -> None:
    loose = NormalizeOptions(numeric_tolerance=0.01)
    assert compare(100, 100.9, options=NORMALIZE, units=UNITS) is Comparison.DIFFERENT
    assert compare(100, 100.9, options=loose, units=UNITS) is Comparison.EQUAL
    assert compare("1 km", "1005 m", options=loose, units=UNITS) is Comparison.EQUAL
    assert compare(100, 102, options=loose, units=UNITS) is Comparison.DIFFERENT


def test_negation_words_are_configurable() -> None:
    italian = NormalizeOptions(negation_words=("Non", " mai "))
    assert italian.negation_words == ("non", "mai")
    assert compare("non certificato", "certificato", options=italian, units=UNITS) is (
        Comparison.NEGATION
    )
    assert compare("not certified", "certified", options=italian, units=UNITS) is (
        Comparison.DIFFERENT
    )


def test_parse_quantity() -> None:
    found = parse_quantity("2.5 km", UNITS)
    assert found is not None
    assert found.dimension == "length"
    assert found.unit == "km"
    assert found.magnitude == 2500
    plain = parse_quantity("42", UNITS)
    assert plain is not None
    assert plain.dimension is None
    assert parse_quantity("5 parsecs", UNITS) is None
    assert parse_quantity("1,000 m", UNITS) is None
    assert parse_quantity("about 5 km", UNITS) is None
    assert parse_quantity("  5 km  ", UNITS) is not None


def test_parse_quantity_stays_linear_on_long_input() -> None:
    hostile = "1" + " " * 50_000 + "x" + " " * 50_000 + "1"
    started = time.perf_counter()
    assert parse_quantity(hostile, UNITS) is None
    assert parse_quantity(hostile, UNITS, max_length=1_000_000) is None
    assert time.perf_counter() - started < 1.0
    assert parse_quantity("5 km", UNITS, max_length=3) is None


@pytest.mark.parametrize("text", ["1e999999999 km", "1e-999999999 km", "9e65 m", "1e999999999"])
def test_parse_quantity_refuses_huge_exponents(text: str) -> None:
    assert parse_quantity(text, UNITS) is None


def test_compare_never_raises_on_huge_numbers() -> None:
    assert compare("1e999999999 km", "1 km", options=NORMALIZE, units=UNITS) is (
        Comparison.DIFFERENT
    )
    loose = NormalizeOptions(numeric_tolerance=0.5)
    assert compare(1e308, -1e308, options=loose, units=UNITS) is Comparison.DIFFERENT


def test_normalize_claims_groups_and_signals() -> None:
    found = normalize_claims(
        [
            claim("1", "5 km", "source:a"),
            claim("2", "5000 m", "source:b"),
            claim("3", "certified", "source:a"),
            claim("4", "not certified", "source:c"),
        ],
        options=NORMALIZE,
        units=UNITS,
    )
    assert [[c.claim_id for c in g] for g in found.groups] == [["1", "2"], ["3"], ["4"]]
    assert found.signals == {Signal.UNIT.value, Signal.NEGATION.value}
    assert found.differs
    same = normalize_claims(
        [claim("1", "5 km"), claim("2", "5000 m")], options=NORMALIZE, units=UNITS
    )
    assert not same.differs


def test_units_file_replaces_the_table(tmp_path: Path) -> None:
    path = tmp_path / "units.yaml"
    path.write_text("length:\n  league: {factor: 4828, aliases: [leagues]}\n  m: {factor: 1}\n")
    table = UnitTable.load(path)
    assert compare("1 league", "4828 m", options=NORMALIZE, units=table) is Comparison.EQUAL
    assert parse_quantity("1 km", table) is None


@pytest.mark.parametrize(
    "text",
    [
        "- just a list\n",
        "length: [m]\n",
        "length:\n  m: {aliases: [meter]}\n",
        "length:\n  m: {factor: -1}\n",
        "length:\n  m: {factor: abc}\n",
        "length:\n  m: {factor: 1, aliases: meter}\n",
    ],
)
def test_bad_units_files_are_refused(tmp_path: Path, text: str) -> None:
    path = tmp_path / "units.yaml"
    path.write_text(text)
    with pytest.raises(UnitTableError):
        UnitTable.load(path)
    with pytest.raises(UnitTableError, match="cannot read"):
        UnitTable.load(tmp_path / "missing.yaml")


# Options ------------------------------------------------------------------------------------


def test_options_defaults_and_stage_order() -> None:
    options = ReconcileOptions()
    assert options.hold_auto_accept
    assert options.enabled
    assert options.stages == (Stage.IDENTITY, Stage.VALUES, Stage.NORMALIZE, Stage.SEMANTIC)
    assert options.runs(Stage.VALUES)
    assert not options.runs(Stage.SEMANTIC)
    reordered = ReconcileOptions.model_validate({"stages": ["normalize", "identity"]})
    assert reordered.stages == (Stage.IDENTITY, Stage.NORMALIZE)
    assert not reordered.runs(Stage.VALUES)
    assert not ReconcileOptions(enabled=False).runs(Stage.IDENTITY)
    semantic = ReconcileOptions.model_validate({"semantic": {"enabled": True}})
    assert semantic.runs(Stage.SEMANTIC)
    with pytest.raises(ValidationError, match="repeat"):
        ReconcileOptions.model_validate({"stages": ["values", "values"]})
    with pytest.raises(ValidationError):
        ReconcileOptions.model_validate({"max_claims_per_candidate": 1})


def test_stale_max_age_is_a_duration() -> None:
    assert StaleOptions().max_age_delta is None
    assert StaleOptions(max_age="P30D").max_age_delta == timedelta(days=30)
    assert StaleOptions(max_age="PT1H30M").max_age_delta == timedelta(hours=1, minutes=30)
    with pytest.raises(ValidationError):
        StaleOptions(max_age="30 days")
