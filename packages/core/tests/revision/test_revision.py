from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from lorebase_core.canonical import canonical_bytes, hex
from lorebase_core.revision import (
    ChangesetEntry,
    Revision,
    RevisionStats,
    changeset_between,
    changeset_stats,
    changeset_tree_changes,
    format_timestamp,
    next_generation,
    revision_id,
)
from lorebase_core.tree import InMemoryNodeStore, tree_apply

GOLDEN = json.loads(
    (Path(__file__).parents[1] / "canonical" / "vectors" / "object_hashes.json").read_text(
        encoding="utf-8"
    )
)

ROOT = bytes(32)
PARENT = bytes(range(32))


def _fields(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "parents": [PARENT],
        "root": ROOT,
        "domain": "supply",
        "author": {"principal": "usr_1", "on_behalf_of": None},
        "message": "Sync erp.vendors run 7",
        "origin": {"type": "source_sync", "ref": "run_01J"},
        "created_at": datetime(2026, 10, 20, 9, 12, 44, 123456, tzinfo=UTC),
        "stats": {"partitions_changed": 2, "changes": 1840},
    }
    fields.update(overrides)
    return fields


def test_matches_golden_revision_vector() -> None:
    vector = next(v for v in GOLDEN["vectors"] if v["object_type"] == "revision")
    revision = Revision.model_validate(vector["value"])
    assert canonical_bytes(revision.canonical_payload()) == vector["canonical"].encode()
    assert hex(revision_id(revision)) == vector["hash"]


def test_canonical_payload_shape() -> None:
    payload = Revision.model_validate(_fields()).canonical_payload()
    assert (
        canonical_bytes(payload)
        == (
            '{"author":{"principal":"usr_1"},"created_at":"2026-10-20T09:12:44.123456Z",'
            '"domain":"supply","message":"Sync erp.vendors run 7",'
            '"origin":{"ref":"run_01J","type":"source_sync"},'
            f'"parents":["{PARENT.hex()}"],"root":"{ROOT.hex()}",'
            '"stats":{"changes":1840,"partitions_changed":2}}'
        ).encode()
    )


def test_id_stable_for_identical_content() -> None:
    a = Revision.model_validate(_fields())
    b = Revision.model_validate(_fields(parents=[PARENT.hex()], root=ROOT.hex()))
    assert revision_id(a) == revision_id(b)
    shifted = _fields(
        created_at=datetime(2026, 10, 20, 11, 12, 44, 123456, tzinfo=timezone(timedelta(hours=2)))
    )
    assert revision_id(Revision.model_validate(shifted)) == revision_id(a)


@pytest.mark.parametrize(
    "overrides",
    [
        {"parents": []},
        {"parents": [PARENT, ROOT]},
        {"root": PARENT},
        {"domain": "hr"},
        {"author": {"principal": "usr_2"}},
        {"author": {"principal": "usr_1", "on_behalf_of": "agt_1"}},
        {"message": "other"},
        {"origin": {"type": "operation", "ref": "run_01J"}},
        {"origin": {"type": "source_sync"}},
        {"created_at": datetime(2026, 10, 20, 9, 12, 44, 123457, tzinfo=UTC)},
        {"stats": {"partitions_changed": 2, "changes": 1841}},
    ],
)
def test_id_changes_with_any_field(overrides: dict[str, Any]) -> None:
    base = revision_id(Revision.model_validate(_fields()))
    assert revision_id(Revision.model_validate(_fields(**overrides))) != base


def test_parent_order_matters() -> None:
    a = Revision.model_validate(_fields(parents=[PARENT, ROOT]))
    b = Revision.model_validate(_fields(parents=[ROOT, PARENT]))
    assert revision_id(a) != revision_id(b)


@pytest.mark.parametrize(
    "overrides",
    [
        {"parents": [PARENT, PARENT]},
        {"root": b"short"},
        {"root": "ABCD"},
        {"created_at": datetime(2026, 10, 20, 9, 12, 44)},
        {"domain": ""},
        {"stats": {"partitions_changed": -1, "changes": 0}},
        {"unexpected": 1},
    ],
)
def test_invalid_revisions_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Revision.model_validate(_fields(**overrides))


def test_revision_is_immutable() -> None:
    revision = Revision.model_validate(_fields())
    with pytest.raises(ValidationError):
        revision.__setattr__("message", "changed")


def test_generation_numbers() -> None:
    assert next_generation([]) == 1
    assert next_generation([1]) == 2
    assert next_generation([3, 7, 2]) == 8


def test_timestamp_format() -> None:
    assert format_timestamp(datetime(5, 1, 2, 3, 4, 5, tzinfo=UTC)) == "0005-01-02T03:04:05.000000Z"
    with pytest.raises(ValueError, match="time zone"):
        format_timestamp(datetime(2026, 1, 1))


def test_changeset_round_trip() -> None:
    store = InMemoryNodeStore()
    h = [bytes([i]) * 32 for i in range(4)]
    parent = tree_apply(store, None, [("k:a", "1", h[0]), ("k:a", "2", h[1])])
    child = tree_apply(store, parent, [("k:a", "1", h[2]), ("k:a", "2", None), ("k:b", "x", h[3])])
    entries = changeset_between(store, parent, child)
    assert sorted((e.partition, e.key, e.old_hash, e.new_hash) for e in entries) == [
        ("k:a", "1", h[0], h[2]),
        ("k:a", "2", h[1], None),
        ("k:b", "x", None, h[3]),
    ]
    assert tree_apply(store, parent, changeset_tree_changes(entries)) == child
    assert changeset_stats(entries) == RevisionStats(partitions_changed=2, changes=3)


def test_changeset_entry_validation() -> None:
    with pytest.raises(ValueError, match="must change"):
        ChangesetEntry("k:a", "1", None, None)
    with pytest.raises(ValueError, match="bytes"):
        ChangesetEntry("k:a", "1", None, b"short")
    entry = ChangesetEntry("k:a", "1", None, bytes(32), {"att_1": "prv_1"})
    assert entry == ChangesetEntry("k:a", "1", None, bytes(32))
