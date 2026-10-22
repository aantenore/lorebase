"""Bundles never bring plain values into vault Attributes, and the vault entries of a bundle
are checked (design 13.5)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest

from lorebase_core.model import vault_reference
from lorebase_store.bundle import (
    BundleError,
    _check_vault_values,
    _collect_holders,
    _Import,
    _vault_attributes,
)
from lorebase_store.vault.transfer import (
    Holder,
    KeyLinks,
    LineageEntry,
    TombstoneEntry,
    VaultEntry,
    VaultTransferError,
    holders_problem,
    parse_entry,
    parse_lineage,
    parse_tombstone,
    resolved_legacy,
)

KIND = "knd_01JB8Z00000000000000000001"
EMAIL = "att_01JB8Z00000000000000000002"
NAME = "att_01JB8Z00000000000000000003"
TOKEN = "vlt_" + "a" * 32


class _Lines:
    def __init__(self, objects: list[dict[str, Any]]) -> None:
        self._objects = objects

    def lines(self, base: str) -> Iterator[dict[str, Any]]:
        assert base == "objects"
        return iter(self._objects)


def _kind() -> dict[str, Any]:
    return {
        "type": "schema.kind",
        "body": {
            "id": KIND,
            "attributes": {EMAIL: {"id": EMAIL, "vault": True}, NAME: {"id": NAME}},
        },
    }


def _record(values: dict[str, Any]) -> dict[str, Any]:
    return {"type": "record", "hash": "h", "body": {"kind": KIND, "key": "P-1", "values": values}}


def test_vault_attributes_hold_references_only_on_import() -> None:
    vaulted = _vault_attributes(cast(_Import, _Lines([_kind(), _record({})])))
    assert vaulted == {KIND: {EMAIL}}
    _check_vault_values(_record({EMAIL: vault_reference(TOKEN), NAME: "Ada"}), vaulted)
    _check_vault_values(_record({NAME: "Ada"}), vaulted)
    with pytest.raises(BundleError, match="not a vault reference"):
        _check_vault_values(_record({EMAIL: "ada@example.com"}), vaulted)
    with pytest.raises(BundleError, match="not a vault reference"):
        _check_vault_values(_record({EMAIL: {"$vault": "forged"}}), vaulted)


_DROP = object()


def _entry(**changes: Any) -> dict[str, Any]:
    line: dict[str, Any] = {
        "token": TOKEN,
        "kind": KIND,
        "attribute": EMAIL,
        "record_key": "P-1",
        "value": "ada@example.com",
    }
    line.update(changes)
    return {k: v for k, v in line.items() if v is not _DROP}


def test_vault_entries_of_a_bundle_are_checked() -> None:
    assert parse_entry(_entry()) == VaultEntry(TOKEN, KIND, EMAIL, "P-1", "ada@example.com")
    assert parse_entry(_entry(value=_DROP, erased=True)).erased
    for line, reason in (
        (_entry(token="vlt_short"), "malformed token"),
        (_entry(record_key=7), "must be non-empty strings"),
        (_entry(erased=True), "either a value or erased"),
        (_entry(value=_DROP), "either a value or erased"),
        (_entry(value=None), "empty or a vault reference"),
        (_entry(value=vault_reference(TOKEN)), "empty or a vault reference"),
        (_entry(extra=1), "unexpected fields"),
    ):
        with pytest.raises(VaultTransferError, match=reason):
            parse_entry(line)


def test_records_linked_by_key_changes_may_hold_one_token() -> None:
    holders: dict[str, set[Holder]] = {}
    _collect_holders(_record({EMAIL: vault_reference(TOKEN), NAME: "Ada"}), holders)
    renamed = _record({EMAIL: vault_reference(TOKEN)})
    renamed["body"]["key"] = "P-2"
    _collect_holders(renamed, holders)
    assert holders == {TOKEN: {Holder(KIND, EMAIL, "P-1"), Holder(KIND, EMAIL, "P-2")}}

    links = KeyLinks()
    problem = holders_problem(TOKEN, KIND, EMAIL, "P-1", holders[TOKEN], links)
    assert problem is not None
    assert "P-2" in problem
    links.link(LineageEntry(KIND, "P-2", "P-1"))
    assert holders_problem(TOKEN, KIND, EMAIL, "P-1", holders[TOKEN], links) is None
    links.link(LineageEntry(KIND, "P-3", "P-2"))  # linked through P-2
    assert holders_problem(TOKEN, KIND, EMAIL, "P-3", holders[TOKEN], links) is None
    assert holders_problem(TOKEN, KIND, NAME, "P-1", holders[TOKEN], links) is not None
    assert "no Record object" in str(holders_problem(TOKEN, KIND, EMAIL, "P-1", (), links))


def test_lineage_and_tombstones_of_a_bundle_are_checked() -> None:
    lineage = {"kind": KIND, "record_key": "P-2", "previous_key": "P-1"}
    assert parse_lineage(lineage) == LineageEntry(KIND, "P-2", "P-1")
    with pytest.raises(VaultTransferError, match="to itself"):
        parse_lineage({**lineage, "previous_key": "P-2"})
    with pytest.raises(VaultTransferError, match="non-empty strings"):
        parse_lineage({**lineage, "kind": ""})

    tombstone = {
        "kind": KIND,
        "attribute": EMAIL,
        "record_key": None,
        "fingerprint": "ab" * 32,
        "fingerprint_key": "1/2",
    }
    assert parse_tombstone(tombstone, {"1"}).fingerprint == bytes.fromhex("ab" * 32)
    assert parse_tombstone(tombstone, {"2", "1"}).record_key is None
    with pytest.raises(VaultTransferError, match="token key '1/2'"):
        parse_tombstone(tombstone, {"7"})
    for changed, reason in (
        ({"fingerprint_key": None}, "does not say which token key"),
        ({"fingerprint_key": "1"}, "token key '1'"),
        ({"fingerprint": "ab"}, "malformed fingerprint"),
        ({"record_key": ""}, "record_key"),
    ):
        with pytest.raises(VaultTransferError, match=reason):
            parse_tombstone({**tombstone, **changed}, {"1"})

    # A legacy tombstone (no fingerprint_key) is exported under the current token key, scheme 1.
    legacy = TombstoneEntry(KIND, EMAIL, "P-1", bytes(32), None)
    assert resolved_legacy(legacy, "4").fingerprint_key == "4/1"
    assert parse_tombstone(resolved_legacy(legacy, "4").as_json(), {"4"}).record_key == "P-1"


def test_moved_entries_and_oversized_lineage_groups() -> None:
    moved = parse_entry(_entry(moved_to="P-2"))
    assert moved.moved_to == "P-2"
    assert moved.as_json()["moved_to"] == "P-2"
    assert "moved_to" not in parse_entry(_entry()).as_json()
    for value in ("P-1", "", 3):
        with pytest.raises(VaultTransferError, match="moved_to"):
            parse_entry(_entry(moved_to=value))

    links = KeyLinks()
    for index in range(3):
        links.link(LineageEntry(KIND, f"P-{index + 1}", f"P-{index}"))
    links.link(LineageEntry(KIND, "Q-1", "Q-0"))
    assert links.oversized(3) == 1  # P-0..P-3 (4 keys); Q-0, Q-1 stay below
    assert links.oversized(4) == 0
