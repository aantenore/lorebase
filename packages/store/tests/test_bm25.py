"""Translation of tsqueries into BM25 queries (`lorebase_store.bm25`), without a database."""

from __future__ import annotations

from typing import Any

import pytest

from lorebase_store import bm25


def term(word: str) -> dict[str, Any]:
    return {"term": {"field": "terms", "value": word}}


ALL = {"all": None}


@pytest.mark.parametrize(
    ("tsquery", "match", "exact"),
    [
        ("'a'", term("a"), True),
        ("'a' & 'b' & 'c'", {"boolean": {"must": [term("a"), term("b"), term("c")]}}, True),
        (
            "'a' | 'b' & !'c'",
            {
                "boolean": {
                    "should": [
                        term("a"),
                        {"boolean": {"must": [term("b")], "must_not": [term("c")]}},
                    ]
                }
            },
            True,
        ),
        ("!'a'", {"boolean": {"must": [ALL], "must_not": [term("a")]}}, True),
        ("!'a' & !'b'", {"boolean": {"must": [ALL], "must_not": [term("a"), term("b")]}}, True),
        (
            "'a' | !'b'",
            {
                "boolean": {
                    "should": [term("a"), {"boolean": {"must": [ALL], "must_not": [term("b")]}}]
                }
            },
            True,
        ),
        # Quotes and backslashes as tsquery prints them.
        ("'it''s' & 'x\\\\y'", {"boolean": {"must": [term("it's"), term("x\\y")]}}, True),
        # Phrases: positional in the index, rechecked against the vector.
        (
            "'a' <-> 'b' <-> 'c'",
            {"phrase": {"field": "terms", "phrases": ["a", "b", "c"], "slop": 0}},
            False,
        ),
        ("'a' <2> 'b'", {"boolean": {"must": [term("a"), term("b")]}}, False),
        (
            "( 'a' | 'b' ) <-> 'c'",
            {"boolean": {"must": [{"boolean": {"should": [term("a"), term("b")]}}, term("c")]}},
            False,
        ),
        # A negation inside a phrase excludes a word at one position, not from the row.
        ("'a' <-> !'b'", term("a"), False),
        ("'a' <-> ( 'b' & !'c' )", {"boolean": {"must": [term("a"), term("b")]}}, False),
        # The negation of a superset has no bound: dropped, then rechecked.
        ("!( 'a' <-> 'b' ) & 'c'", term("c"), False),
        ("!( 'a' <-> 'b' )", ALL, False),
        ("'pre':* & 'x'", term("x"), False),
        ("'w':A", ALL, False),
        ("'a' | 'pre':*", ALL, False),
    ],
)
def test_translate(tsquery: str, match: dict[str, Any], exact: bool) -> None:
    found = bm25.translate(tsquery)
    assert found is not None
    assert (found.match, found.exact) == (match, exact)


def test_translate_refuses_what_the_functions_refuse() -> None:
    assert bm25.translate("") is None
    deep = "'x'"
    for n in range(40):  # alternating AND and OR: nothing to flatten
        deep = f"'w{n}' {'&' if n % 2 else '|'} ( {deep} )"
    assert bm25.translate(deep) is None
    wide = " | ".join(f"'w{n}'" for n in range(300))
    assert bm25.translate(wide) is None
    assert bm25.translate(" | ".join(f"'w{n}'" for n in range(200))) is not None


def test_corrected_only_excludes_the_query_as_written() -> None:
    written = bm25.Bm25Query(term("gama"), True)
    corrected = bm25.Bm25Query({"boolean": {"should": [term("gama"), term("gamma")]}}, True)
    assert bm25.corrected_only(written, corrected) == bm25.Bm25Query(
        {"boolean": {"must": [corrected.match], "must_not": [written.match]}}, True
    )
    phrase = bm25.Bm25Query(term("gama"), False)
    assert bm25.corrected_only(phrase, corrected) == bm25.Bm25Query(corrected.match, False)


def test_index_statement() -> None:
    sql = bm25.index_sql("mat_records_p0", "mat_records_p0_bm25_idx", concurrently=True)
    assert sql.startswith("CREATE INDEX CONCURRENTLY IF NOT EXISTS mat_records_p0_bm25_idx ")
    assert "USING bm25 ((key::pdb.literal), " in sql
    assert "(lb_search_terms(search)::pdb.whitespace('alias=terms'))" in sql
    parent = bm25.index_sql("mat_records", bm25.INDEX, only=True)
    assert f"{bm25.INDEX} ON ONLY mat_records USING bm25" in parent
