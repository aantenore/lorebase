"""Three-way merge of ``text`` values: tokenizer, diff3 and the data merge step (design 6.8)."""

from __future__ import annotations

import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from lorebase_core.merge import ConflictKind, MergePolicy, ResolutionChoice, Side
from lorebase_core.merge.text_merge import (
    MAX_COST,
    MAX_TOKENS,
    HunkStatus,
    TextGranularity,
    TextMerge,
    merge_text,
    tokenize,
)
from lorebase_core.model import Record
from lorebase_core.tree import kind_partition
from lorebase_core.types import TypeSpec

from .support import (
    COUNTRY,
    SUPPLIER,
    Edit,
    Fork,
    attempt,
    base_schema,
    fork,
    merged,
    replace_attribute,
    resolve,
    supplier,
)

BASE = "One is first. Two is second. Three is third.\n\nFour opens another paragraph. Five ends it."
SENTENCE, PARAGRAPH = TextGranularity.SENTENCE, TextGranularity.PARAGRAPH


def run(
    base: str,
    ours: str,
    theirs: str,
    granularity: TextGranularity = SENTENCE,
) -> TextMerge:
    result = merge_text(base, ours, theirs, granularity=granularity)
    assert result is not None
    return result


# Tokenizer -------------------------------------------------------------------------------


@pytest.mark.parametrize("granularity", [SENTENCE, PARAGRAPH])
@pytest.mark.parametrize(
    "text",
    [
        "",
        "plain",
        BASE,
        "A.  B!\tC?\n\n\n D…  E",
        "\n\nleading and trailing\n\n",
        "Version 3.14 is out. See e.g. the notes.\r\n\r\nNext.",
        'He said "stop." Then left.',
        "no end",
    ],
)
def test_tokens_concatenate_to_the_text(text: str, granularity: TextGranularity) -> None:
    assert "".join(tokenize(text, granularity)) == text


@pytest.mark.parametrize(
    "text",
    [
        "." * 200_000,
        "a." + ")" * 200_000,
        "a." + ")" * 200_000 + " b.",
        ".)" * 100_000,
        "\n" + " " * 200_000,
        "\n" + " " * 200_000 + "x",
        "\n \t" * 70_000,
        "x" + "\n" * 200_000,
        "a." + " " * 200_000 + "b",
    ],
)
@pytest.mark.parametrize("granularity", [SENTENCE, PARAGRAPH])
def test_tokenizing_long_runs_is_fast_and_exact(text: str, granularity: TextGranularity) -> None:
    started = time.monotonic()
    tokens = tokenize(text, granularity)
    assert time.monotonic() - started < 2
    assert "".join(tokens) == text


def test_merging_a_long_punctuation_run_is_fast() -> None:
    base = "." * 100_000
    started = time.monotonic()
    result = merge_text(base, base + " ours", base + "!", max_chars=10**6)
    assert time.monotonic() - started < 5
    assert result is None or sides(result) == (base, base + " ours", base + "!")


def test_sentence_ends_after_a_closer_still_split() -> None:
    assert tokenize("Wait...) ok. Next.) Done.  End", SENTENCE) == [
        "Wait...)",
        " ",
        "ok.",
        " ",
        "Next.)",
        " ",
        "Done.",
        "  ",
        "End",
    ]
    assert tokenize("a.). b", SENTENCE) == ["a.).", " ", "b"]


def test_whitespace_between_sentences_is_a_token_of_its_own() -> None:
    assert tokenize("One. Two!  Three?\n\nFour", SENTENCE) == [
        "One.",
        " ",
        "Two!",
        "  ",
        "Three?",
        "\n\n",
        "Four",
    ]


def test_a_full_stop_inside_a_number_does_not_split() -> None:
    assert tokenize("Pi is 3.14 roughly.", SENTENCE) == ["Pi is 3.14 roughly."]


def test_paragraphs_split_on_blank_lines() -> None:
    assert tokenize("a. b.\n\nc.\n \nd", PARAGRAPH) == ["a. b.", "\n\n", "c.", "\n \n", "d"]


def test_a_single_line_break_stays_in_the_paragraph() -> None:
    assert tokenize("line one\nline two", PARAGRAPH) == ["line one\nline two"]


# diff3 -----------------------------------------------------------------------------------


def sides(result: TextMerge) -> tuple[str, str, str]:
    hunks = result.hunks
    return (
        "".join(h.base for h in hunks),
        "".join(h.ours for h in hunks),
        "".join(h.theirs for h in hunks),
    )


def test_a_change_on_one_side_is_taken() -> None:
    ours = BASE.replace("Two is second.", "Two is 2nd.")
    result = run(BASE, ours, BASE)
    assert result.clean
    assert result.text == ours
    assert run(BASE, BASE, ours).text == ours


def test_changes_in_different_sentences_merge() -> None:
    ours = BASE.replace("One is first.", "One is 1st.")
    theirs = BASE.replace("Five ends it.", "Five ends it for good.")
    result = run(BASE, ours, theirs)
    assert result.clean
    assert result.text == BASE.replace("One is first.", "One is 1st.").replace(
        "Five ends it.", "Five ends it for good."
    )
    assert (result.ours_changes, result.theirs_changes) == (1, 1)
    assert run(BASE, theirs, ours).text == result.text


def test_the_same_sentence_changed_differently_conflicts() -> None:
    ours = BASE.replace("Two is second.", "Two is 2nd.")
    theirs = BASE.replace("Two is second.", "Two is the second one.")
    result = run(BASE, ours, theirs)
    assert not result.clean
    conflicts = [h for h in result.hunks if h.status is HunkStatus.CONFLICT]
    assert len(conflicts) == 1
    hunk = conflicts[0]
    assert (hunk.base, hunk.ours, hunk.theirs, hunk.merged) == (
        "second.",
        "2nd.",
        "the second one.",
        None,
    )
    assert sides(result) == (BASE, ours, theirs)


def test_the_same_change_on_both_sides_is_taken_once() -> None:
    ours = BASE.replace("Two is second.", "Two is 2nd.")
    result = run(BASE, ours, ours)
    assert result.clean
    assert result.text == ours


def test_neighbouring_sentences_rewritten_on_each_side_merge() -> None:
    ours = BASE.replace("One is first.", "One is 1st.")
    theirs = BASE.replace("Two is second.", "Two is 2nd.")
    result = run(BASE, ours, theirs)
    assert result.clean
    assert result.text == ours.replace("Two is second.", "Two is 2nd.")
    assert sides(result) == (BASE, ours, theirs)
    assert run(BASE, theirs, ours).text == result.text


def test_changes_sharing_a_token_conflict() -> None:
    ours = BASE.replace("One is first. Two is second.", "One and two.")
    theirs = BASE.replace("Two is second.", "Two is 2nd.")
    result = run(BASE, ours, theirs)
    assert not result.clean
    assert sides(result) == (BASE, ours, theirs)


def test_both_sides_appending_different_sentences_conflict() -> None:
    ours = BASE + " Ours adds this."
    theirs = BASE + " Theirs adds that."
    assert not run(BASE, ours, theirs).clean


def test_identical_insertions_at_the_same_place_collapse() -> None:
    ours = BASE.replace("One is first. ", "One is first. New one. ")
    assert run(BASE, ours, ours).text == ours


def test_an_insertion_at_the_edge_of_the_other_sides_change_conflicts() -> None:
    appended = BASE + " Ours adds this."
    rewritten_last = BASE.replace("Five ends it.", "Five ends it again.")
    assert not run(BASE, appended, rewritten_last).clean
    assert not run(BASE, rewritten_last, appended).clean
    prepended = "Ours adds this. " + BASE
    rewritten_first = BASE.replace("One is first.", "One is 1st.")
    assert not run(BASE, prepended, rewritten_first).clean
    assert not run(BASE, rewritten_first, prepended).clean
    # Away from the insertion, the same rewrite merges.
    far = BASE.replace("Three is third.", "Three is 3rd.")
    assert run(BASE, appended, far).clean


def test_appending_after_the_last_sentence_conflicts_on_the_insertion_only() -> None:
    result = run("S1.", "S1. A.", "S1. B.")
    assert [(h.status, h.base, h.ours, h.theirs, h.merged) for h in result.hunks] == [
        (HunkStatus.CLEAN, "S1.", "S1.", "S1.", "S1."),
        (HunkStatus.CONFLICT, "", " A.", " B.", None),
    ]


def test_a_conflict_hunk_drops_the_text_all_sides_share() -> None:
    base = "Intro. The delay was long today. Outro."
    ours = base.replace("was long", "was short")
    theirs = base.replace("was long", "was very long indeed")
    result = run(base, ours, theirs)
    hunks = [(h.status.value, h.base, h.ours, h.theirs) for h in result.hunks]
    assert hunks == [
        ("clean", "Intro. The delay was ", "Intro. The delay was ", "Intro. The delay was "),
        ("conflict", "long", "short", "very long indeed"),
        ("clean", " today. Outro.", " today. Outro.", " today. Outro."),
    ]
    assert sides(result) == (base, ours, theirs)


def test_shared_text_is_never_cut_inside_a_word() -> None:
    result = run("The cat sat.", "The cart sat.", "The cast sat.")
    (conflict,) = [h for h in result.hunks if h.status is HunkStatus.CONFLICT]
    assert (conflict.base, conflict.ours, conflict.theirs) == ("cat", "cart", "cast")
    assert sides(result) == ("The cat sat.", "The cart sat.", "The cast sat.")


def test_one_side_deleting_and_the_other_editing_the_same_sentence_conflicts() -> None:
    ours = BASE.replace("Two is second. ", "")
    theirs = BASE.replace("Two is second.", "Two is 2nd.")
    assert not run(BASE, ours, theirs).clean


def test_both_sides_inserting_at_the_same_place_differently_conflicts() -> None:
    ours = BASE.replace("Two is second. ", "Two is second. Ours adds this. ")
    theirs = BASE.replace("Two is second. ", "Two is second. Theirs adds that. ")
    assert not run(BASE, ours, theirs).clean


def test_insertions_in_different_places_merge() -> None:
    ours = BASE.replace("One is first. ", "One is first. Ours adds this. ")
    theirs = BASE + " Theirs adds that."
    result = run(BASE, ours, theirs)
    assert result.clean
    assert result.text == ours + " Theirs adds that."


def test_hunks_cover_each_side_exactly() -> None:
    ours = BASE.replace("One is first.", "One is 1st.")
    theirs = BASE.replace("Two is second.", "Two is 2nd.")
    assert sides(run(BASE, ours, theirs)) == (BASE, ours, theirs)


def test_unchanged_stretches_form_one_hunk() -> None:
    ours = BASE.replace("One is first.", "One is 1st.")
    result = run(BASE, ours, BASE)
    assert [h.status for h in result.hunks] == [HunkStatus.CLEAN] * len(result.hunks)
    assert len(result.hunks) == 2
    assert result.hunks[1].merged == BASE[len("One is first.") :]


def test_paragraph_granularity_merges_by_paragraph() -> None:
    text = "First one. First two.\n\nSecond one. Second two.\n\nThird one. Third two."
    ours = text.replace("First one.", "First 1.")
    far = text.replace("Third two.", "Third 2.")
    near = text.replace("First two.", "First 2.")
    assert run(text, ours, far, granularity=PARAGRAPH).clean
    # Two sentences of one paragraph are one unit under paragraph granularity.
    assert not run(text, ours, near, granularity=PARAGRAPH).clean
    assert run(text, ours, near, granularity=SENTENCE).clean  # neighbouring sentences
    assert run(text, near, far, granularity=SENTENCE).clean


def test_whitespace_is_not_normalized() -> None:
    ours = BASE.replace("One is first. ", "One is first.   ")
    theirs = BASE.replace("Five ends it.", "Five ends it!")
    result = run(BASE, ours, theirs)
    assert result.clean
    assert "first.   Two" in result.text
    assert result.text.endswith("Five ends it!")


def test_text_above_the_character_cap_is_not_merged() -> None:
    assert merge_text(BASE, BASE, BASE, max_chars=len(BASE) * 3 - 1) is None
    assert merge_text(BASE, BASE, BASE, max_chars=len(BASE) * 3) is not None


def test_text_above_the_token_cap_is_not_merged() -> None:
    long = "a. " * (MAX_TOKENS // 3 + 1)
    assert merge_text(long, long, long, max_chars=10**9) is None


def test_repetitive_text_above_the_cost_bound_is_skipped_quickly() -> None:
    size = int(MAX_COST**0.5) + 1
    base = "ok. " * size
    started = time.monotonic()
    assert merge_text(base, base + "a. ", base + "b. ") is None
    assert time.monotonic() - started < 1


def test_repetitive_text_over_the_cost_bound_keeps_the_plain_conflict() -> None:
    size = int(MAX_COST**0.5) + 1
    base = "ok. " * size
    scenario = _scenario(base + "a. ", base + "b. ", base=base)
    (conflict,) = attempt(scenario.merge()).conflicts
    assert conflict.details == {}


_WORDS = st.sampled_from(["alpha. ", "beta! ", "gamma? ", "delta.\n\n", "eps "])


@settings(max_examples=60, deadline=None)
@given(
    base=st.lists(_WORDS, max_size=12),
    ours=st.lists(_WORDS, max_size=12),
    theirs=st.lists(_WORDS, max_size=12),
    granularity=st.sampled_from([SENTENCE, PARAGRAPH]),
)
def test_hunks_always_reproduce_the_three_values(
    base: list[str], ours: list[str], theirs: list[str], granularity: TextGranularity
) -> None:
    b, o, t = "".join(base), "".join(ours), "".join(theirs)
    result = run(b, o, t, granularity=granularity)
    assert sides(result) == (b, o, t)
    assert run(b, o, b, granularity=granularity).text == o
    assert run(b, b, t, granularity=granularity).text == t


# Policy ----------------------------------------------------------------------------------


def test_text_merge_is_on_by_default_and_scoped_like_prefer() -> None:
    policy = MergePolicy()
    assert policy.merges_text(SUPPLIER, COUNTRY)
    assert policy.text_merge_granularity is SENTENCE
    off = MergePolicy(text_merge=False, text_merge_elements={SUPPLIER: True})
    assert off.merges_text(SUPPLIER, COUNTRY)
    assert not off.merges_text("knd_other", COUNTRY)
    most_specific = MergePolicy(
        text_merge_elements={SUPPLIER: False}, text_merge_attributes={COUNTRY: True}
    )
    assert most_specific.merges_text(SUPPLIER, COUNTRY)
    assert not most_specific.merges_text(SUPPLIER, "att_other")


def test_the_policy_rejects_unknown_granularity_and_a_zero_cap() -> None:
    with pytest.raises(ValidationError):
        MergePolicy.model_validate({"text_merge_granularity": "word"})
    with pytest.raises(ValidationError):
        MergePolicy(text_merge_max_chars=0)


# Data merge ------------------------------------------------------------------------------

PARTITION = kind_partition(SUPPLIER)


def _record(text: str) -> Record:
    return supplier("S-1", vat="IT001", country=text, rating="3")


def _scenario(ours: str, theirs: str, kind: str = "text", base: str = BASE) -> Fork:
    schema = replace_attribute(base_schema(), SUPPLIER, COUNTRY, type=TypeSpec.of(kind))
    return fork(
        Edit().upsert(_record(ours)),
        Edit().upsert(_record(theirs)),
        schema=schema,
        data=[_record(base)],
    )


OURS = BASE.replace("One is first.", "One is 1st.")
THEIRS = BASE.replace("Five ends it.", "Five ends it for good.")
BOTH = OURS.replace("Five ends it.", "Five ends it for good.")


def _country(scenario: Fork, root: bytes) -> object:
    values = scenario.repo.values(root, SUPPLIER, "S-1")
    assert values is not None
    return values[COUNTRY]


def test_text_edited_in_different_places_merges_and_is_recorded() -> None:
    scenario = _scenario(OURS, THEIRS)
    result = merged(scenario.merge())
    assert _country(scenario, result.root) == BOTH
    (auto,) = result.auto_resolutions
    assert auto.kind is ConflictKind.ATTRIBUTE_VALUE
    assert auto.resolver == "policy:text_merge"
    assert auto.choice is ResolutionChoice.CUSTOM
    assert (auto.partition, auto.key, auto.attribute) == (PARTITION, "S-1", COUNTRY)
    assert auto.details == {"ours_hunks": 1, "theirs_hunks": 1}
    assert _country(scenario, merged(scenario.reverse()).root) == BOTH


def test_the_merged_text_is_a_change_against_ours_with_its_own_provenance_slot() -> None:
    scenario = _scenario(OURS, THEIRS)
    result = merged(scenario.merge())
    (entry,) = [e for e in result.changes if e.partition == PARTITION]
    assert entry.key == "S-1"
    assert entry.provenance == {}


def test_overlapping_edits_report_the_conflict_with_hunks() -> None:
    ours = BASE.replace("Two is second.", "Two is 2nd.")
    theirs = BASE.replace("Two is second.", "Two is the second one.")
    scenario = _scenario(ours, theirs)
    result = attempt(scenario.merge())
    (conflict,) = result.conflicts
    assert conflict.kind is ConflictKind.ATTRIBUTE_VALUE
    assert result.auto_resolutions == ()
    raw = conflict.details["text_hunks"]
    assert isinstance(raw, list)
    hunks = [h for h in raw if isinstance(h, dict)]
    assert len(hunks) == len(raw)
    assert [h["status"] for h in hunks].count("conflict") == 1
    for side, value in (("base", BASE), ("ours", ours), ("theirs", theirs)):
        assert "".join(str(h[side]) for h in hunks) == value
    assert all((h["merged"] is None) == (h["status"] == "conflict") for h in hunks)
    # The fingerprint does not depend on the text merge.
    off = attempt(scenario.merge(policy=MergePolicy(text_merge=False)))
    assert off.conflicts[0].fingerprint == conflict.fingerprint
    assert "text_hunks" not in off.conflicts[0].details


def test_a_person_still_resolves_a_text_conflict() -> None:
    ours = BASE.replace("Two is second.", "Two is 2nd.")
    theirs = BASE.replace("Two is second.", "Two is the second one.")
    scenario = _scenario(ours, theirs)
    (conflict,) = attempt(scenario.merge()).conflicts
    custom = resolve(conflict.fingerprint, "custom", value="Custom text.")
    result = merged(scenario.merge(resolutions=[custom]))
    assert _country(scenario, result.root) == "Custom text."
    picked = resolve(conflict.fingerprint, "theirs")
    result = merged(scenario.merge(resolutions=[picked]))
    assert _country(scenario, result.root) == theirs


def test_policies_decide_a_text_conflict_the_merge_leaves_open() -> None:
    ours = BASE.replace("Two is second.", "Two is 2nd.")
    theirs = BASE.replace("Two is second.", "Two is the second one.")
    scenario = _scenario(ours, theirs)
    result = merged(scenario.merge(policy=MergePolicy(prefer=Side.THEIRS)))
    assert _country(scenario, result.root) == theirs
    assert {a.resolver for a in result.auto_resolutions} == {"policy:prefer"}


def test_a_clean_text_merge_comes_before_the_other_policies() -> None:
    scenario = _scenario(OURS, THEIRS)
    result = merged(scenario.merge(policy=MergePolicy(prefer=Side.THEIRS)))
    assert _country(scenario, result.root) == BOTH
    assert {a.resolver for a in result.auto_resolutions} == {"policy:text_merge"}


def test_a_disabled_policy_keeps_the_conflict() -> None:
    scenario = _scenario(OURS, THEIRS)
    result = attempt(scenario.merge(policy=MergePolicy(text_merge=False)))
    (conflict,) = result.conflicts
    assert conflict.kind is ConflictKind.ATTRIBUTE_VALUE
    assert conflict.details == {}


def test_an_attribute_override_enables_or_disables_the_merge() -> None:
    scenario = _scenario(OURS, THEIRS)
    on = MergePolicy(text_merge=False, text_merge_attributes={COUNTRY: True})
    assert _country(scenario, merged(scenario.merge(policy=on)).root) == BOTH
    off = MergePolicy(text_merge_attributes={COUNTRY: False})
    attempt(scenario.merge(policy=off))
    element_off = MergePolicy(text_merge_elements={SUPPLIER: False})
    attempt(scenario.merge(policy=element_off))


def test_paragraph_granularity_from_the_policy() -> None:
    base = "First one. First two.\n\nSecond one.\n\nThird one. Third two."
    ours = base.replace("First one.", "First 1.")
    theirs = base.replace("Third two.", "Third 2.")
    scenario = _scenario(ours, theirs, base=base)
    expected = ours.replace("Third two.", "Third 2.")
    for granularity in (SENTENCE, PARAGRAPH):
        policy = MergePolicy(text_merge_granularity=granularity)
        assert _country(scenario, merged(scenario.merge(policy=policy)).root) == expected
    same_paragraph = base.replace("First two.", "First 2.")
    scenario = _scenario(ours, same_paragraph, base=base)
    attempt(scenario.merge(policy=MergePolicy(text_merge_granularity=PARAGRAPH)))


def test_the_size_cap_from_the_policy_leaves_the_conflict() -> None:
    scenario = _scenario(OURS, THEIRS)
    result = attempt(scenario.merge(policy=MergePolicy(text_merge_max_chars=10)))
    assert result.conflicts[0].details == {}


@pytest.mark.parametrize("kind", ["string", "uri"])
def test_other_textual_types_are_not_merged(kind: str) -> None:
    ours = OURS.replace("\n", " ")
    theirs = THEIRS.replace("\n", " ")
    scenario = _scenario(ours, theirs, kind=kind, base=BASE.replace("\n", " "))
    result = attempt(scenario.merge())
    assert result.conflicts[0].details == {}


def test_a_text_added_on_both_sides_is_not_merged() -> None:
    schema = replace_attribute(base_schema(), SUPPLIER, COUNTRY, type=TypeSpec.of("text"))
    scenario = fork(
        Edit().upsert(supplier("S-9", vat="IT009", country=OURS)),
        Edit().upsert(supplier("S-9", vat="IT009", country=THEIRS)),
        schema=schema,
    )
    result = attempt(scenario.merge())
    (conflict,) = [c for c in result.conflicts if c.kind is ConflictKind.ADD_ADD]
    assert conflict.details == {}
