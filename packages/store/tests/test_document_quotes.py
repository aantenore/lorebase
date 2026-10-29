"""Quotes of a purged document and signals of version checks (design 9.2)."""

from __future__ import annotations

from lorebase_core.model import Sensitivity
from lorebase_store.repositories.quotes import erase_quotes
from lorebase_store.repositories.reconciliation_candidates import _merge_signals, wrap_signals

PASSAGE = {"document_id": "doc_1", "chunk": 0, "start": 3, "end": 9, "quote": "secret"}


def test_quotes_of_the_document_are_erased_and_locators_kept() -> None:
    other = {**PASSAGE, "document_id": "doc_2"}
    node = {
        "evidence": {"att_a": PASSAGE, "att_b": other},
        "claims": [{"evidence": {**PASSAGE, "quote": {"$sensitive": {"tags": [], "value": "x"}}}}],
        "signals": {"evidence_gone": {"document": "doc_1", "quote": "secret"}},
    }
    erased, changed = erase_quotes(node, "doc_1", "[erased]")
    assert changed
    assert erased["evidence"]["att_a"] == {**PASSAGE, "quote": "[erased]"}
    assert erased["evidence"]["att_b"] == other
    assert erased["claims"][0]["evidence"]["quote"] == "[erased]"
    assert erased["claims"][0]["evidence"]["start"] == 3
    assert erased["signals"]["evidence_gone"]["quote"] == "[erased]"
    again, changed_again = erase_quotes(erased, "doc_1", "[erased]")
    assert again == erased
    assert not changed_again


def test_signal_quotes_are_wrapped_and_left_out_for_vault() -> None:
    signal = {"evidence_gone": {"document": "doc_1", "quote": "PL123"}, "stale": ["max_age"]}
    tags = (Sensitivity.PII,)
    wrapped = wrap_signals(signal, tags)
    assert wrapped["evidence_gone"]["quote"] == {"$sensitive": {"tags": ["pii"], "value": "PL123"}}
    assert wrapped["stale"] == ["max_age"]
    assert wrap_signals(signal, tags, vault=True)["evidence_gone"]["quote"] is None
    assert wrap_signals(signal, ())["evidence_gone"]["quote"] == "PL123"


def test_stale_reasons_are_united_when_a_candidate_is_seen_again() -> None:
    merged = _merge_signals({"stale": ["max_age"]}, {"stale": ["evidence_gone"], "x": 1})
    assert merged == {"stale": ["max_age", "evidence_gone"], "x": 1}
