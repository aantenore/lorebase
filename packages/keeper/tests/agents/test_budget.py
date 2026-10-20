"""Token budget of context packs: estimate, trimming order, minimality and determinism."""

from __future__ import annotations

import math

import pytest

from lorebase_core.model import Schema
from lorebase_keeper.agents.budget import (
    DEFAULT_TRIM_ORDER,
    Draft,
    TrimStep,
    apply,
    estimate_tokens,
    fit,
    units,
)
from lorebase_keeper.agents.markdown import markdown_measure
from lorebase_keeper.agents.pack import budget_json, json_measure

CPT = 4.0


def size(draft: Draft, schema: Schema) -> int:
    return sum(json_measure(schema)(draft).values())


def tokens(draft: Draft, schema: Schema) -> int:
    return estimate_tokens(size(draft, schema), CPT)


@pytest.mark.parametrize(("chars", "expected"), [(0, 0), (1, 1), (4, 1), (5, 2), (400, 100)])
def test_estimate_rounds_up(chars: int, expected: int) -> None:
    assert estimate_tokens(chars, CPT) == expected


def test_a_pack_within_budget_is_untouched(draft: Draft, schema: Schema) -> None:
    result = fit(
        draft,
        budget_tokens=tokens(draft, schema),
        chars_per_token=CPT,
        measure=json_measure(schema),
    )
    assert result.draft == draft
    assert result.trims == ()
    assert not result.over_budget


def test_steps_follow_the_default_order_and_remove_as_little_as_needed(
    draft: Draft, schema: Schema
) -> None:
    full = tokens(draft, schema)
    for budget in range(full - 1, 0, -max(full // 40, 1)):
        result = fit(draft, budget_tokens=budget, chars_per_token=CPT, measure=json_measure(schema))
        steps = [t.step for t in result.trims]
        assert steps == sorted(steps, key=DEFAULT_TRIM_ORDER.index)
        assert result.draft.seeds == draft.seeds
        if result.over_budget:
            assert all(units(result.draft, step) == 0 for step in DEFAULT_TRIM_ORDER)
            continue
        assert size(result.draft, schema) <= budget * CPT
        # Minimal: the last step removing one unit fewer would not fit.
        replayed = draft
        for trim in result.trims[:-1]:
            replayed = apply(replayed, trim.step, trim.removed)
        last = result.trims[-1]
        assert size(apply(replayed, last.step, last.removed - 1), schema) > budget * CPT


def test_trimming_is_deterministic(draft: Draft, schema: Schema) -> None:
    budget = tokens(draft, schema) // 2
    first = fit(draft, budget_tokens=budget, chars_per_token=CPT, measure=json_measure(schema))
    second = fit(draft, budget_tokens=budget, chars_per_token=CPT, measure=json_measure(schema))
    assert first == second


def test_lineage_goes_first_then_changes_oldest_first(draft: Draft, schema: Schema) -> None:
    without_lineage = apply(draft, TrimStep.LINEAGE, units(draft, TrimStep.LINEAGE))
    budget = tokens(without_lineage, schema)
    result = fit(draft, budget_tokens=budget, chars_per_token=CPT, measure=json_measure(schema))
    assert result.trims[0].step is TrimStep.LINEAGE
    assert result.draft.changes == draft.changes

    one_change = tokens(apply(without_lineage, TrimStep.RECENT_CHANGES, 1), schema)
    result = fit(draft, budget_tokens=one_change, chars_per_token=CPT, measure=json_measure(schema))
    assert [t.step for t in result.trims] == [TrimStep.LINEAGE, TrimStep.RECENT_CHANGES]
    assert result.draft.changes == draft.changes[1:]  # type: ignore[index]


def test_far_neighbors_lose_values_then_go_with_their_links(draft: Draft, schema: Schema) -> None:
    stripped = apply(draft, TrimStep.FAR_NEIGHBOR_VALUES, 1)
    assert [n.values is None for n in stripped.neighbors] == [False, False, False, True]

    dropped = apply(draft, TrimStep.FAR_NEIGHBORS, 1)
    assert [n.ref for n in dropped.neighbors] == ["Contract/C-3001", "Part/P-10028", "Plant/PL-03"]
    assert all("Plant/PL-05" not in (link.source, link.target) for link in dropped.links)
    assert len(dropped.links) == len(draft.links) - 1

    near = apply(draft, TrimStep.NEAR_NEIGHBORS, 2)
    assert [n.ref for n in near.neighbors] == ["Plant/PL-03", "Plant/PL-05"]
    assert near.links == ()  # every Link touched P-10028 or C-3001


def test_seeds_are_never_trimmed(draft: Draft, schema: Schema) -> None:
    result = fit(draft, budget_tokens=1, chars_per_token=CPT, measure=json_measure(schema))
    assert result.over_budget
    assert result.draft.seeds == draft.seeds
    assert result.draft.neighbors == ()
    assert result.draft.links == ()
    assert not result.draft.schema
    assert result.draft.changes == ()
    assert result.draft.lineage == ()


def test_configured_order_limits_the_steps(draft: Draft, schema: Schema) -> None:
    result = fit(
        draft,
        budget_tokens=1,
        chars_per_token=CPT,
        measure=json_measure(schema),
        order=(TrimStep.SCHEMA,),
    )
    assert [t.step for t in result.trims] == [TrimStep.SCHEMA]
    assert result.draft.neighbors == draft.neighbors


def test_markdown_is_measured_on_its_own_text(draft: Draft, schema: Schema) -> None:
    json_chars = size(draft, schema)
    markdown_chars = sum(markdown_measure(schema)(draft).values())
    assert markdown_chars != json_chars
    result = fit(
        draft,
        budget_tokens=math.ceil(markdown_chars / CPT),
        chars_per_token=CPT,
        measure=markdown_measure(schema),
    )
    assert result.trims == ()


def test_budget_block_accounts_sections_and_omissions(draft: Draft, schema: Schema) -> None:
    budget = tokens(apply(draft, TrimStep.LINEAGE, 2), schema)
    result = fit(draft, budget_tokens=budget, chars_per_token=CPT, measure=json_measure(schema))
    block = budget_json(result, draft, budget_tokens=budget, chars_per_token=CPT, capped=False)
    assert block["limit_tokens"] == budget
    assert block["used_tokens"] <= budget
    assert block["trimmed"] == [{"step": "lineage", "removed": 2}]
    assert block["omitted"] == {"lineage": 2}
    assert set(block["sections"]) == {
        "seeds",
        "neighbors",
        "links",
        "schema",
        "recent_changes",
        "lineage",
    }
    assert block["over_budget"] is False
