"""Bundles never bring plain values into vault Attributes (design 13.5)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest

from lorebase_core.model import vault_reference
from lorebase_store.bundle import BundleError, _check_vault_values, _Import, _vault_attributes

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
