"""Detection stages over gathered claims (design 10.6): values, normalization, stale, duplicates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from lorebase_core.model import PeriodMapping
from lorebase_core.model.source import AttributeMapping
from lorebase_core.reconcile import (
    CandidateType,
    Claim,
    DuplicateRule,
    Outcome,
    ReconcileOptions,
    Stage,
    StaleOptions,
    StaleReason,
    Verdict,
    duplicate_draft,
    pairs,
    stale_reasons,
    stale_verdict,
    value_verdict,
)

from ..model.supply import ident

KIND = ident("knd", 1)
ATTR = ident("att", 1)
OPTIONS = ReconcileOptions()


def claim(
    claim_id: str,
    value: Any,
    source: str | None = "source:a",
    at: str = "2026-10-01T00:00:00Z",
    **period: str | None,
) -> Claim:
    return Claim(
        claim_id=claim_id, value=value, source=source, provenance_id=claim_id, at=at, **period
    )


def verdict(
    claims: list[Claim],
    *,
    textual: bool = True,
    valid_time: bool = False,
    options: ReconcileOptions = OPTIONS,
    proposal_id: str | None = None,
) -> Verdict:
    return value_verdict(
        claims,
        branch="main",
        kind_id=KIND,
        key="S-1",
        attribute_id=ATTR,
        valid_time=valid_time,
        textual=textual,
        options=options,
        proposal_id=proposal_id,
    )


def test_agreement_raises_nothing() -> None:
    same = [claim("p1", "Milan", "source:a"), claim("p2", "Milan", "source:b")]
    assert verdict(same).outcome is Outcome.AGREE
    one_source = [claim("p1", "Milan", "source:a"), claim("p2", "Rome", "source:a")]
    assert verdict(one_source).outcome is Outcome.AGREE


def test_different_sources_raise_a_contradiction() -> None:
    found = verdict(
        [claim("p1", "Milan", "source:a"), claim("p2", "Rome", "principal:usr_1")],
        proposal_id="prp_1",
    )
    assert found.raised
    assert found.draft is not None
    assert found.draft.type is CandidateType.CONTRADICTION
    assert found.draft.keys == ("S-1",)
    assert found.draft.proposal_id == "prp_1"
    assert {c.claim_id for c in found.draft.claims} == {"p1", "p2"}


def test_normalization_filters_and_marks() -> None:
    units = [claim("p1", "5 km", "source:a"), claim("p2", "5000 m", "source:b")]
    assert verdict(units).outcome is Outcome.FILTERED
    # Not a textual Attribute: no normalization, the values differ.
    assert verdict(units, textual=False).raised
    # The stage left out: the values differ.
    without = ReconcileOptions(stages=(Stage.IDENTITY, Stage.VALUES))
    assert verdict(units, options=without).raised
    negation = verdict([claim("p1", "certified", "source:a"), claim("p2", "not certified", "b:x")])
    assert negation.draft is not None
    assert negation.draft.signals == {"negation": True}


def test_values_stage_left_out_raises_nothing() -> None:
    options = ReconcileOptions(stages=(Stage.IDENTITY,))
    found = verdict([claim("p1", "a", "source:a"), claim("p2", "b", "source:b")], options=options)
    assert found.outcome is Outcome.AGREE
    assert (
        verdict(
            [claim("p1", "a", "source:a"), claim("p2", "b", "source:b")],
            options=ReconcileOptions(enabled=False),
        ).outcome
        is Outcome.AGREE
    )


def test_disjoint_periods_are_superseded() -> None:
    older = claim(
        "p1",
        "Milan",
        "source:a",
        valid_from="2020-01-01T00:00:00Z",
        valid_to="2024-01-01T00:00:00Z",
    )
    newer = claim("p2", "Rome", "source:b", valid_from="2024-01-01T00:00:00Z")
    found = verdict([older, newer], valid_time=True)
    assert found.draft is not None
    assert found.draft.type is CandidateType.SUPERSEDED
    assert found.draft.signals["periods"] is True
    plain = verdict([older, newer]).draft
    assert plain is not None
    assert plain.type is CandidateType.CONTRADICTION


def test_stale_reasons() -> None:
    now = datetime(2026, 10, 29, tzinfo=UTC)
    current = claim("p1", "Milan", "source:a", at="2026-01-01T00:00:00Z")
    assert stale_reasons(current, now=now, max_age=None) == []
    assert stale_reasons(current, now=now, max_age=timedelta(days=30)) == [StaleReason.MAX_AGE]
    ended = claim("p1", "Milan", "source:a", valid_to="2026-06-01T00:00:00Z")
    assert stale_reasons(ended, now=now, max_age=None) == [StaleReason.PERIOD_ENDED]
    pending = claim("p2", "Rome", "source:a", at="2026-10-01T00:00:00Z")
    other = claim("p3", "Turin", "source:b", at="2026-10-01T00:00:00Z")
    assert stale_reasons(current, now=now, max_age=None, pending=[other]) == []
    found = stale_verdict(
        current,
        branch="main",
        kind_id=KIND,
        key="S-1",
        attribute_id=ATTR,
        now=now,
        max_age=None,
        pending=[pending, other],
    )
    assert found.draft is not None
    assert found.draft.type is CandidateType.STALE
    assert [c.claim_id for c in found.draft.claims] == ["p1", "p2"]
    assert found.draft.signals == {"stale": ["pending_claim"]}


def test_stale_option_reads_iso_durations() -> None:
    assert StaleOptions(max_age="P30D").max_age_delta == timedelta(days=30)


def test_duplicate_pairs_fold_into_one_candidate() -> None:
    first = duplicate_draft(
        branch="main", kind_id=KIND, keys=("S-2", "S-1"), rule=DuplicateRule.TRIGRAM, score=0.91
    )
    second = duplicate_draft(
        branch="main",
        kind_id=KIND,
        keys=("S-1", "S-2"),
        rule=DuplicateRule.NORMALIZED,
        attribute_id=ATTR,
    )
    assert first.fingerprint == second.fingerprint
    assert first.keys == ("S-1", "S-2")
    assert first.signals == {"trigram": {"similarity": 0.91}}
    assert second.signals == {"normalized": {"attribute": ATTR}}
    with pytest.raises(ValueError, match="two keys"):
        duplicate_draft(
            branch="main", kind_id=KIND, keys=("S-1", "S-1"), rule=DuplicateRule.TRIGRAM
        )


def test_pairs_are_bounded() -> None:
    assert pairs(["c", "a", "b", "a"], limit=10) == [("a", "b"), ("a", "c"), ("b", "c")]
    assert pairs(["a", "b", "c"], limit=1) == [("a", "b")]


def test_period_mappings_need_one_input() -> None:
    assert PeriodMapping(column="since").column == "since"
    with pytest.raises(ValueError, match="exactly one"):
        PeriodMapping()
    mapping = AttributeMapping(column="city", valid_from=PeriodMapping(expression="row.since"))
    assert mapping.valid_to is None
    # Unset periods leave the mapping's content as before.
    assert "valid_from" not in AttributeMapping(column="city").content()
