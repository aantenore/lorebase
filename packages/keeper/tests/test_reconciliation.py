"""Reconciliation decisions without a database: policies over claims, masking of what is shown,
and validity periods of `update_{kind}` (design 10.6)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import JsonValue

from lorebase_core.merge import MergePolicy
from lorebase_core.model import Schema, Sensitivity, load_schema
from lorebase_core.reconcile import value_hash
from lorebase_keeper.errors import ProblemError
from lorebase_keeper.reading.sensitivity import Clearance, Masking
from lorebase_keeper.services.operation_catalog import (
    PARAMETER_INVALID,
    VALID,
    bind,
    builtin_definition,
    plan_edits,
)
from lorebase_keeper.services.reconciliation import (
    ReconciliationPolicy,
    apply_policy,
    authority_ranking,
    raw_value,
    shown,
)
from lorebase_store.repositories import CandidateRow, wrap_claim

SUPPLY_SCHEMA = Path(__file__).resolve().parents[3] / "examples" / "supply" / "schema.yaml"
NOW = datetime(2026, 10, 20, tzinfo=UTC)


@pytest.fixture(scope="module")
def schema() -> Schema:
    document: dict[str, Any] = yaml.safe_load(SUPPLY_SCHEMA.read_text())
    del document["operations"]
    del document["derivations"]["single_sourced"]
    del document["kinds"]["Part"]["attributes"]["single_sourced"]
    document["kinds"]["Supplier"]["attributes"]["city"]["valid_time"] = True
    return load_schema(yaml.safe_dump(document))


def claim(claim_id: str, value: Any, source: str, at: str, **extra: Any) -> dict[str, Any]:
    return {
        "claim_id": claim_id,
        "value": value,
        "value_hash": value_hash(value),
        "source": source,
        "provenance_id": claim_id,
        "at": at,
        **extra,
    }


def candidate(
    *claims: dict[str, Any], kind: str = "knd_x", attribute: str | None = "att_x"
) -> CandidateRow:
    return CandidateRow(
        id="rcn_1",
        branch="main",
        type="contradiction",
        status="open",
        fingerprint=b"\0" * 32,
        kind_id=kind,
        keys=["S-1"],
        attribute_id=attribute,
        claims=list(claims),
        signals={},
        claim_count=len(claims),
        proposal_id=None,
        decision=None,
        decided_by=None,
        decided_at=None,
        created_at=NOW,
        updated_at=NOW,
    )


ERP = claim("p1", "Poznan", "source:src_erp", "2026-01-01T00:00:00Z")
CRM = claim("p2", "Gdansk", "source:src_crm", "2026-03-01T00:00:00Z")
DOC = claim(
    "p3", "Lodz", "document:doc_1", "2026-02-01T00:00:00Z", valid_from="2026-06-01T00:00:00Z"
)


def test_authority_folds_the_ranking_over_every_claim() -> None:
    ranked = candidate(ERP, CRM, DOC)
    chosen = apply_policy(
        ranked,
        ReconciliationPolicy.AUTHORITY,
        ranking=("source:src_crm", "source:src_erp"),
        current_hash=None,
    )
    assert chosen is not None
    assert chosen["claim_id"] == "p2"
    # Two unranked claims tie while nothing ranks above them: nothing is decided.
    assert (
        apply_policy(
            candidate(CRM, DOC),
            ReconciliationPolicy.AUTHORITY,
            ranking=("source:src_erp",),
            current_hash=None,
        )
        is None
    )
    # A ranked claim settles the tie of two unranked ones.
    chosen = apply_policy(
        candidate(CRM, DOC, ERP),
        ReconciliationPolicy.AUTHORITY,
        ranking=("source:src_erp",),
        current_hash=None,
    )
    assert chosen is not None
    assert chosen["claim_id"] == "p1"


def test_newest_wins_prefers_the_period_start() -> None:
    chosen = apply_policy(
        candidate(ERP, CRM, DOC), ReconciliationPolicy.NEWEST_WINS, ranking=(), current_hash=None
    )
    assert chosen is not None
    assert chosen["claim_id"] == "p3"


def test_prefer_needs_two_claims() -> None:
    two = candidate(ERP, CRM)
    current = value_hash("Poznan")
    kept = apply_policy(two, ReconciliationPolicy.PREFER_CURRENT, ranking=(), current_hash=current)
    other = apply_policy(
        two, ReconciliationPolicy.PREFER_INCOMING, ranking=(), current_hash=current
    )
    assert kept is not None
    assert kept["claim_id"] == "p1"
    assert other is not None
    assert other["claim_id"] == "p2"
    with pytest.raises(ProblemError):
        apply_policy(
            candidate(ERP, CRM, DOC),
            ReconciliationPolicy.PREFER_CURRENT,
            ranking=(),
            current_hash=current,
        )


def test_authority_ranking_puts_the_attribute_authority_first(schema: Schema) -> None:
    kind = schema.kind_by_api_name("Supplier")
    assert kind is not None
    city = kind.attribute_by_api_name("city")
    assert city is not None
    policy = MergePolicy(authority=("source:src_crm",))
    plain = city.model_copy(update={"authority": None})
    assert authority_ranking(policy, plain) == ("source:src_crm",)
    authoritative = city.model_copy(update={"authority": "src_erp"})
    assert authority_ranking(policy, authoritative) == ("source:src_erp", "source:src_crm")


def test_sensitive_claims_are_shown_by_clearance(schema: Schema) -> None:
    kind = schema.kind_by_api_name("Supplier")
    assert kind is not None
    vat = kind.attribute_by_api_name("vat_number")
    assert vat is not None
    tags = (Sensitivity.CONFIDENTIAL,)
    evidence = {"document_id": "doc_1", "start": 1, "end": 6, "quote": "PL123"}
    stored = candidate(
        wrap_claim(
            claim("p1", "PL123", "source:a", "2026-01-01T00:00:00Z", evidence=evidence), tags
        ),
        wrap_claim(claim("p2", "PL999", "source:b", "2026-01-02T00:00:00Z"), tags),
        kind=kind.id,
        attribute=vat.id,
    )
    blind = Masking(Clearance()).over(schema)
    seen = shown(stored, schema, blind)
    assert [c["value"] for c in seen["claims"]] == [blind.masked, blind.masked]
    assert seen["claims"][0]["evidence"] == {
        "document_id": "doc_1",
        "start": 1,
        "end": 6,
        "quote": blind.masked,
    }
    cleared = Masking(Clearance(everywhere=frozenset(tags))).over(schema)
    plain = shown(stored, schema, cleared)
    assert [c["value"] for c in plain["claims"]] == ["PL123", "PL999"]
    assert plain["claims"][0]["evidence"]["quote"] == "PL123"
    narrowed = Masking(Clearance(kinds={Sensitivity.CONFIDENTIAL: frozenset({"knd_other"})}))
    assert shown(stored, schema, narrowed.over(schema))["claims"][0]["value"] == blind.masked
    assert raw_value(stored.claims[0]["value"]) == "PL123"


async def test_update_takes_validity_periods_of_valid_time_values(schema: Schema) -> None:
    update = builtin_definition(schema, "update_supplier")
    assert update is not None
    assert VALID in {p.name for p in update.parameters}
    plain = builtin_definition(schema, "update_part")
    assert plain is not None
    assert VALID not in {p.name for p in plain.parameters}
    kind = schema.kind_by_api_name("Supplier")
    assert kind is not None
    city = kind.attribute_by_api_name("city")
    assert city is not None

    async def read(kind_id: str, keys: Any) -> dict[str, Any]:
        return {k: {city.id: "Poznan"} for k in keys}

    good = bind(
        update,
        {
            "key": "S-1",
            "city": "Lodz",
            VALID: {"city": {"from": "2026-06-01T00:00:00Z", "to": None}},
        },
    )
    plan = await plan_edits(update, good.values, read)
    assert plan.findings == []
    assert plan.periods == {city.id: ("2026-06-01T00:00:00.000000Z", None)}

    cases: list[JsonValue] = [
        {"country": {"from": "2026-01-01T00:00:00Z"}},  # not a valid_time Attribute
        {
            "city": {"from": "2026-06-01T00:00:00Z", "to": "2026-01-01T00:00:00Z"}
        },  # ends before it starts
        {"city": {"since": "2026-06-01T00:00:00Z"}},
    ]
    for given in cases:
        bad = bind(update, {"key": "S-1", "city": "Lodz", VALID: given})
        found = await plan_edits(update, bad.values, read)
        assert [f.code for f in found.findings] == [PARAMETER_INVALID], given
    unset = bind(update, {"key": "S-1", VALID: {"city": {"from": "2026-06-01T00:00:00Z"}}})
    assert [f.code for f in (await plan_edits(update, unset.values, read)).findings] == [
        PARAMETER_INVALID
    ]
