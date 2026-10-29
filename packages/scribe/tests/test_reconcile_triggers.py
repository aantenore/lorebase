"""Reconciliation triggers without a database (design 10.6): periods of Source mappings and
extraction answers, the hold of contested extraction items, ambiguous mentions, options."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from lorebase_core.model import (
    IdPrefix,
    OperationOrigin,
    PeriodMapping,
    ProvenanceEntry,
    Schema,
    SourceSyncOrigin,
    new_id,
)
from lorebase_core.reconcile import CandidateType, Claim, ReconcileOptions
from lorebase_scribe.extraction.assemble import CurrentState, assemble_items
from lorebase_scribe.extraction.candidates import (
    Evidence,
    MatchCandidate,
    Mention,
    Resolution,
    Resolved,
    ValueCandidate,
)
from lorebase_scribe.extraction.ontology import build_ontology, fragment_schema
from lorebase_scribe.extraction.settings import ExtractionSettings
from lorebase_scribe.extraction.tier2 import Tier2Result, period
from lorebase_scribe.reconcile.detector import Detector, effective_options
from lorebase_scribe.reconcile.extraction import ExtractionCheck, ambiguous_pairs
from lorebase_scribe.reconcile.sweep import stored_claims
from lorebase_scribe.sources.delta import CurrentState as SourceState
from lorebase_scribe.sources.delta import StoredRecord, compute_delta, content_hash
from lorebase_scribe.sources.mapping import RowError, RowMapper
from lorebase_store.materialize.rows import ELEMENT_PROVENANCE_KEY
from lorebase_store.models.enums import ItemDecision
from lorebase_store.repositories.reconciliation_candidates import CandidateRow

NOW = datetime(2026, 10, 29, tzinfo=UTC)
CURRENT_ID = new_id(IdPrefix.PROVENANCE)
SETTINGS = ExtractionSettings(auto_accept_threshold=0.6)
ROW = {
    "supplier_code": "S-1001",
    "legal_name": "Kavsen Alloys",
    "country": "PL",
    "city": "Poznan",
    "tier": "1",
    "risk_score": "32",
    "onboarded_on": "2024-02-26",
    "since": "2025-03-01",
}


def _ids(schema: Schema, name: str) -> tuple[str, str]:
    kind = schema.kind_by_api_name("Supplier")
    assert kind is not None
    attribute = kind.attribute_by_api_name(name)
    assert attribute is not None
    return kind.id, attribute.id


def _valid_city(schema: Schema, *, mapped: bool = True) -> Schema:
    """`Supplier.city` with `valid_time`, its Source mapping reading `since` as valid_from."""
    kind_id, city = _ids(schema, "city")
    kind = schema.kinds[kind_id]
    attribute = kind.attributes[city].model_copy(update={"valid_time": True})
    kind = kind.model_copy(update={"attributes": {**kind.attributes, city: attribute}})
    source = schema.source_by_api_name("approved.suppliers")
    assert source is not None
    if mapped:
        mapping = source.attribute_mapping[city].model_copy(
            update={"valid_from": PeriodMapping(column="since")}
        )
        source = source.model_copy(
            update={"attribute_mapping": {**source.attribute_mapping, city: mapping}}
        )
    return schema.model_copy(
        update={
            "kinds": {**schema.kinds, kind_id: kind},
            "sources": {**schema.sources, source.id: source},
        }
    )


def _mapper(schema: Schema) -> RowMapper:
    source = schema.source_by_api_name("approved.suppliers")
    assert source is not None
    return RowMapper(schema, source)


def _provenance(_row: Any) -> ProvenanceEntry:
    return ProvenanceEntry(
        id=new_id(IdPrefix.PROVENANCE),
        origin=SourceSyncOrigin(source_id="src_01KEZF7C00PMKRJJ98PAYYB4SX", run_id="srn_1"),
        actor="svc_scribe",
        at="2026-10-21T08:00:00.000000Z",
    )


# Source mappings ------------------------------------------------------------------------------


def test_mapped_period_lands_on_its_own_provenance_entry(supply_schema: Schema) -> None:
    schema = _valid_city(supply_schema)
    _kind, city = _ids(schema, "city")
    mapper = _mapper(schema)
    assert "since" in mapper.columns
    mapped = mapper.map(ROW, "suppliers.csv:2")
    assert mapped.periods == {city: ("2025-03-01T00:00:00.000000Z", None)}

    delta = compute_delta(
        mapper, [mapped], SourceState(), delete_missing=False, provenance=_provenance
    )
    (change,) = delta.groups[0].changes
    assert change.provenance is not None
    assert change.periods[city].valid_from == "2025-03-01T00:00:00.000000Z"
    assert change.periods[city].id != change.provenance.id
    attribution = change.attribution()
    assert isinstance(attribution, dict)
    assert attribution[city] == change.periods[city].id
    assert attribution[ELEMENT_PROVENANCE_KEY] == change.provenance.id
    assert {e.id for e in change.entries} == {change.provenance.id, change.periods[city].id}

    # An unchanged value gets no entry of its own, even with a period.
    record = mapper.record(mapped, None)
    stored = {mapped.key: StoredRecord(b"", {**record.values})}
    moved = mapper.map({**ROW, "risk_score": "40"}, "suppliers.csv:2")
    again = compute_delta(
        mapper, [moved], SourceState(records=stored), delete_missing=False, provenance=_provenance
    )
    (update,) = again.groups[0].changes
    assert update.periods == {}
    assert update.attribution() == update.provenance.id  # type: ignore[union-attr]
    assert content_hash(record)


def test_periods_are_checked_and_ignored_without_valid_time(supply_schema: Schema) -> None:
    mapper = _mapper(_valid_city(supply_schema))
    with pytest.raises(RowError, match="valid_from"):
        mapper.map({**ROW, "since": "not a date"}, "x")
    assert mapper.map({**ROW, "since": ""}, "x").periods == {}
    # The flag off: the mapping's period is not read.
    kind_id, city = _ids(supply_schema, "city")
    plain = _valid_city(supply_schema).model_copy(update={"kinds": supply_schema.kinds})
    assert _mapper(plain).map(ROW, "x").periods == {}
    assert city in supply_schema.kinds[kind_id].attributes


# Extraction -------------------------------------------------------------------------------------


def _resolved(kind_id: str, key: str, status: Any = "matched", **extra: Any) -> Resolved:
    mention = Mention(
        kind_id=kind_id,
        text=key,
        start=0,
        end=len(key),
        chunk=0,
        score=1.0,
        tier=0,
        source="key",
        extractor="test",
        document_id="doc_1",
    )
    return Resolved(mention, Resolution(kind_id, status, key, 1.0, "exact", **extra))


def _current_claim(value: str) -> Claim:
    entry = ProvenanceEntry(
        id=CURRENT_ID,
        origin=OperationOrigin(
            invocation_id="inv_1", operation="update_supplier", principal="usr_1"
        ),
        actor="usr_1",
        at="2026-01-01T00:00:00Z",
    )
    return Claim.of_provenance(entry, value)


def test_contested_items_stay_pending_and_raise_candidates(supply_schema: Schema) -> None:
    schema = _valid_city(supply_schema, mapped=False)
    kind_id, city = _ids(schema, "city")
    _kind, legal = _ids(schema, "legal_name")
    detector = Detector(ReconcileOptions(), principal="svc_scribe", clock=lambda: NOW)
    check = ExtractionCheck(
        detector,
        schema,
        "main",
        {
            (kind_id, "S-1013", city): _current_claim("Vienna"),
            (kind_id, "S-1013", legal): _current_claim("Kavsen Alloys"),
        },
        now=NOW,
    )
    known = _resolved(kind_id, "S-1013")
    evidence = Evidence(document_id="doc_1", chunk=0, start=0, end=20, quote="Graz since 2025")
    moved = ValueCandidate(
        known, city, "Graz", evidence, 1.0, 2, "m", "2025-01-01T00:00:00.000000Z"
    )
    spelled = ValueCandidate(known, legal, "KAVSEN  alloys", evidence, 1.0, 2, "m")
    assert check.contests(moved)
    assert not check.contests(spelled)  # equal once normalized

    assembled = assemble_items(
        build_ontology(schema, None),
        [known],
        [moved, spelled],
        [],
        CurrentState(records={(kind_id, "S-1013"): {}}, links=frozenset()),
        job_id="exj_1",
        principal="svc_scribe",
        now=NOW,
        settings=SETTINGS,
        hold=check.contests,
    )
    decisions = {i.subject["attribute"]: i.decision for i in assembled.items}
    assert decisions == {city: ItemDecision.PENDING, legal: ItemDecision.ACCEPTED}
    assert assembled.stats["held_by_reconciliation"] == 1
    held = next(i for i in assembled.items if i.attribute_id == city)
    assert held.evidence is not None
    assert held.evidence["valid_from"] == "2025-01-01T00:00:00.000000Z"

    raised = check.raised(assembled.items, "prp_1", [])
    assert len(raised) == 1
    draft = raised[0].draft
    assert draft.type is CandidateType.CONTRADICTION
    assert draft.proposal_id == "prp_1"
    proposed = next(c for c in draft.claims if c.proposal_item is not None)
    assert proposed.source == "document:doc_1"
    assert proposed.claim_id == f"prp_1/{held.item_id}"
    assert proposed.valid_from == "2025-01-01T00:00:00.000000Z"
    assert proposed.evidence is not None
    assert "valid_from" not in proposed.evidence
    assert raised[0].attribute is not None
    assert raised[0].attribute.id == city


def test_without_hold_contested_items_follow_confidence(supply_schema: Schema) -> None:
    kind_id, city = _ids(supply_schema, "city")
    options = ReconcileOptions(hold_auto_accept=False)
    detector = Detector(options, principal="svc_scribe", clock=lambda: NOW)
    check = ExtractionCheck(
        detector, supply_schema, "main", {(kind_id, "S-1", city): _current_claim("A")}, now=NOW
    )
    assert not check.holds


def test_ambiguous_mentions_pair_their_two_best_candidates() -> None:
    candidates = (
        MatchCandidate("knd_a", "S-2", "Acme", 0.9, "trigram"),
        MatchCandidate("knd_b", "X-1", "Acme", 0.9, "trigram"),
        MatchCandidate("knd_a", "S-1", "ACME", 0.85, "trigram"),
        MatchCandidate("knd_a", "S-3", "Acme Co", 0.8, "trigram"),
    )
    ambiguous = _resolved("knd_a", "S-2", "ambiguous", candidates=candidates)
    matched = _resolved("knd_a", "S-9")
    assert ambiguous_pairs([ambiguous, matched]) == [("knd_a", "S-2", "S-1", 0.85)]


def test_tier2_periods_must_be_written_in_the_quote(supply_schema: Schema) -> None:
    schema = _valid_city(supply_schema, mapped=False)
    kind_id, city = _ids(schema, "city")
    attribute = schema.kinds[kind_id].attributes[city]
    result = Tier2Result()
    quote = "moved to Graz on 2025-03-01"
    assert period(attribute, {"from": "2025-03-01"}, quote, result) == (
        "2025-03-01T00:00:00.000000Z",
        None,
    )
    assert period(attribute, {"from": "2024-01-01"}, quote, result) == (None, None)
    assert result.discarded == {"ungrounded_period": 1}
    plain = supply_schema.kinds[kind_id].attributes[city]
    assert period(plain, {"from": "2025-03-01"}, quote, result) == (None, None)

    answer = fragment_schema(build_ontology(schema, None), [kind_id])
    record = answer["properties"]["records"]["items"]["anyOf"][0]
    values = record["properties"]["values"]["properties"]
    assert "valid" in values["city"]["properties"]
    assert "valid" not in values["legal_name"]["properties"]


# Options and stored claims ---------------------------------------------------------------------


def test_domain_settings_override_the_options() -> None:
    base = ReconcileOptions()
    found = effective_options(base, {"reconciliation": {"hold_auto_accept": False}})
    assert found.hold_auto_accept is False
    assert effective_options(base, {"reconciliation": {"unknown": 1}}) is base
    assert effective_options(base, {"reconciliation": "off"}) is base
    assert effective_options(base, None) is base


def test_stored_claims_are_read_back_unwrapped() -> None:
    claim = _current_claim("Vienna")
    document = {**claim.document(), "value": {"$sensitive": {"tags": ["pii"], "value": "Vienna"}}}
    row = CandidateRow(
        id="rcn_1",
        branch="main",
        type="contradiction",
        status="open",
        fingerprint=b"x",
        kind_id="knd",
        keys=["S-1"],
        attribute_id="att",
        claims=[document, {"claim_id": "broken"}],
        signals={},
        claim_count=2,
        proposal_id=None,
        decision=None,
        decided_by=None,
        decided_at=None,
        created_at=NOW,
        updated_at=NOW,
    )
    assert stored_claims(row) == [claim]
