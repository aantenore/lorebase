"""Vault references (design 6.1, 13.5).

The value of an Attribute stored in the vault is replaced, in Record objects, by a reference
``{"$vault": "<token>"}``. Objects, hashes, diffs and merges see the reference only; the value
lives encrypted in the store's ``vault`` table, and erasing it leaves every Revision intact
while reads of the reference answer that the value was erased.

Tokens are opaque here: the store derives them (``lorebase_store.vault``) so that the same
value of the same Attribute of the same Record gives the same token, which keeps unchanged
values unchanged across writes, branches and merges.
"""

from __future__ import annotations

import re

from pydantic import JsonValue

VAULT_KEY = "$vault"
"""The only member of a vault reference."""

TOKEN_PREFIX = "vlt_"
TOKEN_PATTERN = re.compile(r"vlt_[0-9a-f]{32}")
"""Tokens minted by the store: the prefix and 128 bits in lowercase hex."""


def vault_reference(token: str) -> dict[str, JsonValue]:
    """The canonical value that stands for a vaulted value."""
    return {VAULT_KEY: token}


def vault_token(value: object) -> str | None:
    """The token of a well-formed vault reference; None for any other value."""
    if isinstance(value, dict) and len(value) == 1:
        token = value.get(VAULT_KEY)
        if isinstance(token, str) and TOKEN_PATTERN.fullmatch(token):
            return token
    return None


def looks_like_reference(value: object) -> bool:
    """Whether `value` is shaped like a vault reference, well formed or not."""
    return isinstance(value, dict) and VAULT_KEY in value
