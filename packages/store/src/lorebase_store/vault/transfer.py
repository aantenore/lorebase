"""Vault values carried by a Domain bundle (design 13.5, `lorebase_store.bundle`).

A bundle exported with its vault values holds one entry per token its Record objects reference:
the Kind, Attribute and Record key the token is bound to, and either the opened value or the
mark that it was erased. The values are plain text; only an explicit export writes them.

- `exported_entry` turns what `open_values` answered for a token into an entry; a value whose
  key is not configured cannot be exported.
- `parse_entry` checks an entry read from a bundle: a well-formed token, ids and key as
  strings, and exactly one of a value (never a reference) or the erased mark.
- `restore_entries` stores entries in the target Domain: values are sealed under the target's
  keys with the bundle's token as associated data and get the target's fingerprint, so they
  open, and erasure by value finds them, as if written there; erased entries become erased
  rows. Tokens the target already holds are refused.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import JsonValue
from sqlalchemy import Table, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import looks_like_reference
from lorebase_core.model.vault import TOKEN_PATTERN
from lorebase_store.models import metadata
from lorebase_store.vault.keys import VaultKeys
from lorebase_store.vault.store import VaultRow, VaultState, VaultValue, insert_rows

_VAULT: Table = metadata.tables["vault"]
_BATCH = 500
_FIELDS = frozenset({"token", "kind", "attribute", "record_key", "value", "erased"})


class VaultTransferError(ValueError):
    """A vault entry cannot be exported, is malformed, or cannot be stored."""


@dataclass(frozen=True, slots=True)
class VaultEntry:
    """A token, its binding and its value (`erased`: no value)."""

    token: str
    kind_id: str
    attribute_id: str
    record_key: str
    value: JsonValue = None
    erased: bool = False

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "token": self.token,
            "kind": self.kind_id,
            "attribute": self.attribute_id,
            "record_key": self.record_key,
        }
        if self.erased:
            out["erased"] = True
        else:
            out["value"] = self.value
        return out


def exported_entry(
    token: str, kind_id: str, attribute_id: str, record_key: str, opened: VaultValue
) -> VaultEntry:
    """The entry of a token bound to a Record's Attribute, from what `open_values` answered.
    A token unknown to the vault (imported without its value) is exported as erased."""
    if opened.state is VaultState.UNAVAILABLE:
        raise VaultTransferError(
            f"the value of {token} is sealed under a key that is not configured "
            "(vault.previous_keys)"
        )
    if opened.kind_id is not None and (opened.kind_id, opened.attribute_id) != (
        kind_id,
        attribute_id,
    ):
        raise VaultTransferError(f"{token} is bound to another Kind or Attribute in the vault")
    if opened.state is VaultState.ERASED:
        return VaultEntry(token, kind_id, attribute_id, record_key, erased=True)
    return VaultEntry(token, kind_id, attribute_id, record_key, opened.value)


def parse_entry(line: Mapping[str, Any]) -> VaultEntry:
    """A checked entry of a bundle (module doc)."""
    unknown = set(line) - _FIELDS
    if unknown:
        raise VaultTransferError(f"unexpected fields {sorted(unknown)}")
    token = line.get("token")
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise VaultTransferError(f"malformed token {token!r}")
    binding = [line.get(name) for name in ("kind", "attribute", "record_key")]
    if not all(isinstance(part, str) and part for part in binding):
        raise VaultTransferError(f"{token}: kind, attribute and record_key must be strings")
    kind_id, attribute_id, record_key = (str(part) for part in binding)
    erased = line.get("erased", False)
    if erased is not True and erased is not False:
        raise VaultTransferError(f"{token}: erased must be true when present")
    has_value = "value" in line
    if erased == has_value:
        raise VaultTransferError(f"{token}: give either a value or erased")
    value = line.get("value")
    if has_value and (value is None or looks_like_reference(value)):
        raise VaultTransferError(f"{token}: the value is empty or a vault reference")
    return VaultEntry(token, kind_id, attribute_id, record_key, value, erased)


async def existing_tokens(
    session: AsyncSession, domain_id: str, tokens: Collection[str]
) -> set[str]:
    """The tokens among `tokens` that the Domain's vault already holds."""
    v = _VAULT
    ordered = sorted(tokens)
    found: set[str] = set()
    for start in range(0, len(ordered), 1000):
        rows = await session.execute(
            select(v.c.id).where(
                v.c.domain_id == domain_id, v.c.id.in_(ordered[start : start + 1000])
            )
        )
        found.update(rows.scalars())
    return found


async def restore_entries(
    session: AsyncSession,
    domain_id: str,
    entries: Sequence[VaultEntry],
    keys: VaultKeys | None,
    *,
    at: datetime,
) -> None:
    """Store `entries` in the Domain's vault (module doc)."""
    if not entries:
        return
    values = [e for e in entries if not e.erased]
    if values and keys is None:
        raise VaultTransferError(
            "the bundle holds vault values but the vault is not configured "
            "(vault.token_key, vault.encryption_key)"
        )
    taken = await existing_tokens(session, domain_id, [e.token for e in entries])
    if taken:
        raise VaultTransferError(
            f"the vault already holds {len(taken)} of the bundle's tokens, e.g. {min(taken)}"
        )
    if values:
        assert keys is not None
        rows = [
            VaultRow(
                token=e.token,
                kind_id=e.kind_id,
                attribute_id=e.attribute_id,
                record_key=e.record_key,
                fingerprint=keys.fingerprint(domain_id, e.kind_id, e.attribute_id, e.value),
                ciphertext=keys.seal(domain_id, e.token, e.value),
                key_id=keys.key_id,
                fingerprint_key=keys.fingerprint_key,
            )
            for e in values
        ]
        await insert_rows(session, domain_id, rows, at=at)
    erased = [e for e in entries if e.erased]
    for start in range(0, len(erased), _BATCH):
        await session.execute(
            insert(_VAULT).values(
                [
                    {
                        "domain_id": domain_id,
                        "id": e.token,
                        "kind_id": e.kind_id,
                        "attribute_id": e.attribute_id,
                        "record_key": e.record_key,
                        "fingerprint": None,
                        "value_ciphertext": None,
                        "key_id": None,
                        "created_at": at,
                        "erased_at": at,
                    }
                    for e in erased[start : start + _BATCH]
                ]
            )
        )
