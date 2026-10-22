"""Bundles never bring plain values into vault Attributes, and the vault entries of a bundle
are checked (design 13.5)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest

from lorebase_core.model import vault_reference
from lorebase_store.bundle import (
    BundleError,
    _bind_tokens,
    _check_vault_values,
    _Import,
    _vault_attributes,
)
from lorebase_store.vault import VaultState, VaultValue
from lorebase_store.vault.transfer import (
    VaultEntry,
    VaultTransferError,
    exported_entry,
    parse_entry,
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
        (_entry(record_key=7), "must be strings"),
        (_entry(erased=True), "either a value or erased"),
        (_entry(value=_DROP), "either a value or erased"),
        (_entry(value=None), "empty or a vault reference"),
        (_entry(value=vault_reference(TOKEN)), "empty or a vault reference"),
        (_entry(extra=1), "unexpected fields"),
    ):
        with pytest.raises(VaultTransferError, match=reason):
            parse_entry(line)


def test_exported_entries_follow_the_vault_state() -> None:
    value = VaultValue(VaultState.VALUE, KIND, EMAIL, "ada@example.com")
    assert exported_entry(TOKEN, KIND, EMAIL, "P-1", value).as_json()["value"] == "ada@example.com"
    unknown = exported_entry(TOKEN, KIND, EMAIL, "P-1", VaultValue(VaultState.ERASED))
    assert unknown.as_json() == {
        "token": TOKEN,
        "kind": KIND,
        "attribute": EMAIL,
        "record_key": "P-1",
        "erased": True,
    }
    unavailable = VaultValue(VaultState.UNAVAILABLE, KIND, EMAIL)
    with pytest.raises(VaultTransferError, match="not configured"):
        exported_entry(TOKEN, KIND, EMAIL, "P-1", unavailable)
    elsewhere = VaultValue(VaultState.VALUE, KIND, NAME, "x")
    with pytest.raises(VaultTransferError, match="another Kind or Attribute"):
        exported_entry(TOKEN, KIND, EMAIL, "P-1", elsewhere)


def test_a_token_held_by_two_records_is_refused() -> None:
    bindings: dict[str, Any] = {}
    _bind_tokens(_record({EMAIL: vault_reference(TOKEN), NAME: "Ada"}), bindings)
    assert list(bindings) == [TOKEN]
    other = _record({EMAIL: vault_reference(TOKEN)})
    other["body"]["key"] = "P-2"
    with pytest.raises(BundleError, match="held by two"):
        _bind_tokens(other, bindings)
