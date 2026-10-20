from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lorebase_core.canonical import (
    HASH_SCHEME,
    HASH_SIZE,
    CanonicalError,
    ObjectType,
    canonical_bytes,
    from_hex,
    hex,
    node_hash,
    object_hash,
    revision_hash,
)

GOLDEN = json.loads(
    (Path(__file__).parent / "vectors" / "object_hashes.json").read_text(encoding="utf-8")
)


def test_golden_vectors_lock_the_scheme() -> None:
    # Changing any golden hash requires a new scheme tag.
    assert GOLDEN["scheme"] == HASH_SCHEME == "lb1"


@pytest.mark.parametrize("vector", GOLDEN["vectors"], ids=lambda v: v["object_type"])
def test_golden_object_hashes(vector: dict[str, object]) -> None:
    object_type = str(vector["object_type"])
    assert canonical_bytes(vector["value"]) == str(vector["canonical"]).encode("utf-8")
    assert hex(object_hash(object_type, vector["value"])) == vector["hash"]


def test_golden_vectors_cover_every_object_type() -> None:
    covered = {v["object_type"] for v in GOLDEN["vectors"]}
    assert {t.value for t in ObjectType} | {"revision"} == covered


def test_object_hash_formula() -> None:
    value = {"kind": "k", "key": "1"}
    expected = hashlib.sha256(b"lb1\x00record\x00" + canonical_bytes(value)).digest()
    assert object_hash(ObjectType.RECORD, value) == expected
    assert len(expected) == HASH_SIZE


def test_object_type_is_part_of_the_hash() -> None:
    value = {"id": "x"}
    assert object_hash(ObjectType.SCHEMA_KIND, value) != object_hash(ObjectType.SCHEMA_TRAIT, value)


def test_unknown_object_type_rejected() -> None:
    with pytest.raises(ValueError, match="unknown object type"):
        object_hash("node", {})


def test_revision_and_node_hash_domains() -> None:
    assert revision_hash({"a": 1}) == object_hash("revision", {"a": 1})
    assert node_hash(b"\x01") == hashlib.sha256(b"lb1\x00node\x00\x01").digest()


def test_nulls_omitted_from_objects_but_kept_in_arrays() -> None:
    assert canonical_bytes({"a": None, "b": [None, 1]}) == b'{"b":[null,1]}'
    assert object_hash("record", {"a": "1", "b": None}) == object_hash("record", {"a": "1"})


def test_strings_and_keys_are_nfc_normalized() -> None:
    decomposed = "é"
    composed = "é"
    assert canonical_bytes({decomposed: decomposed}) == canonical_bytes({composed: composed})


def test_keys_colliding_after_nfc_rejected() -> None:
    with pytest.raises(CanonicalError, match="duplicate"):
        canonical_bytes({"é": 1, "é": 2})


def test_hex_round_trip() -> None:
    h = object_hash("link", {"relation": "r", "from": "a", "to": "b"})
    assert from_hex(hex(h)) == h
    with pytest.raises(ValueError):
        from_hex(hex(h).upper())
    with pytest.raises(ValueError):
        from_hex("ab")


@given(st.dictionaries(st.text(max_size=5), st.text(max_size=5) | st.none(), max_size=6))
def test_canonical_bytes_idempotent(mapping: dict[str, str | None]) -> None:
    once = canonical_bytes(mapping)
    assert canonical_bytes(json.loads(once)) == once
