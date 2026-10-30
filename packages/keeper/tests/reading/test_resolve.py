"""Pure parts of resolution (design 8.9): words, plural folding, trigrams, the schema index,
phrases and predicates."""

from __future__ import annotations

from lorebase_core.model.yaml_schema import load_schema
from lorebase_keeper.reading.resolve import (
    Tokens,
    build_index,
    folded,
    name_words,
    phrases_of,
    predicate,
    similarity,
    trigrams,
    variants,
    words_of,
)
from lorebase_keeper.reading.settings import ResolveSettings

RULES = [("s", ""), ("es", "")]

SCHEMA = """
format: lorebase.schema/v1
domain: {id: shop}
kinds:
  PurchaseOrder:
    aliases: [order]
    description: An order placed with a vendor.
    attributes:
      unit_price: {type: "decimal(10,2)", display_name: Unit price, aliases: [cost]}
      status:
        type: {enum: [open, closed]}
        value_aliases: {closed: [done, finished]}
      labels: {type: array<string>}
"""


def test_words_are_normalized() -> None:
    assert words_of("Average PRICE of red-shoes, Café crème!") == [
        "average",
        "price",
        "of",
        "red-shoes",
        "cafe",
        "creme",
    ]
    assert words_of("C++ and S/M at 50%, (rock & roll).") == [
        "c++",
        "and",
        "s/m",
        "at",
        "50%",
        "rock",
        "&",
        "roll",
    ]
    hindi = "".join(chr(c) for c in (0x939, 0x93F, 0x928, 0x94D, 0x926, 0x940))
    assert words_of(f"{hindi} words") == [hindi, "words"]
    assert Tokens(separators="", edges="").words("a,b c.") == ["a,b", "c."]
    assert name_words("unit_price") == "unit price"
    assert name_words("PurchaseOrder") == "purchase order"


def test_plural_rules_fold_both_ways() -> None:
    assert folded("shoes", RULES)[:2] == ["shoes", "shoe"]
    assert "shoes" in folded("shoe", RULES)
    assert folded("shoe", []) == ["shoe"]
    assert "calzatura" in folded("calzature", [("e", "a")])
    assert "red shoe" in variants(("red", "shoes"), RULES)
    assert len(variants(("a" * 5,) * 5, RULES, cap=7)) <= 7


def test_trigram_similarity_follows_pg_trgm() -> None:
    # pg_trgm: similarity('torrino', 'torino') = 0.6666667,
    # similarity('rossso', 'rosso') = 0.85714287
    assert round(similarity(trigrams("torrino"), trigrams("torino")), 4) == 0.6667
    assert round(similarity(trigrams("rossso"), trigrams("rosso")), 3) == 0.857
    assert similarity(frozenset(), trigrams("x")) == 0.0


def test_schema_index_holds_names_aliases_and_descriptions() -> None:
    schema = load_schema(SCHEMA)
    index = build_index(schema)
    order = schema.kind_by_api_name("PurchaseOrder")
    assert order is not None
    assert [e.kind_id for e in index.phrases["purchase order"]] == [order.id]
    assert index.phrases["order"][0].synonym
    assert {e.attribute_id for e in index.phrases["unit price"]} == {
        order.attribute_by_api_name("unit_price").id  # type: ignore[union-attr]
    }
    assert index.phrases["finished"][0].value == "closed"
    assert "vendor" in index.descriptions
    assert "an" not in index.descriptions  # short words are left out


def test_phrases_longest_first() -> None:
    settings = ResolveSettings(max_phrase_words=2, min_word_chars=2)
    phrases = phrases_of(["red", "a", "shoes"], settings)
    assert [p.text for p in phrases] == ["red a", "a shoes", "red", "shoes"]
    raw = phrases_of(["rock", "&", "roll"], settings, "rock & roll")
    assert raw[0].text == "rock & roll"
    assert raw[0].raw is not None
    assert phrases_of(["red"], settings, "red")[0].raw is None


def test_predicates() -> None:
    schema = load_schema(SCHEMA)
    order = schema.kind_by_api_name("PurchaseOrder")
    assert order is not None
    status = order.attribute_by_api_name("status")
    labels = order.attribute_by_api_name("labels")
    assert status is not None
    assert labels is not None
    assert predicate(status, ["open"]) == {"attr": "status", "op": "eq", "value": "open"}
    assert predicate(status, ["open", "closed"]) == {
        "attr": "status",
        "op": "in",
        "value": ["open", "closed"],
    }
    assert predicate(labels, ["a"]) == {"attr": "labels", "op": "contains", "value": "a"}
    assert predicate(labels, ["a", "b"]) == {
        "or": [
            {"attr": "labels", "op": "contains", "value": "a"},
            {"attr": "labels", "op": "contains", "value": "b"},
        ]
    }
