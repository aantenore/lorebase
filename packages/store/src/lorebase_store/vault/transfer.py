"""The vault carried by a Domain bundle (design 13.5, `lorebase_store.bundle`).

A bundle exported with its vault values holds three sections:

- **entries**, one per token its Record objects reference: the Kind, Attribute and Record key
  the vault row binds the token to, and either the opened value or the mark that it was erased.
  A token without a row (imported earlier without its value) is exported as erased, bound to a
  Record holding it. The values are plain text; only an explicit export writes them.
- **lineage**: the key changes of `vault_lineage`. A key change on an erased reference keeps
  the token, so the Records before and after it both hold it; `KeyLinks` links their keys.
- **tombstones**: the fingerprints erasures recorded (`vault_tombstones`), with the token key
  they were made under (`fingerprint_key`, `<token_key_id>/<scheme>`). They hold no value and
  cannot be made again, so a target only takes them when it has that token key id.

`holders_problem` checks that the Records holding a token in a bundle hold it in the bound Kind
and Attribute, under the bound key or one linked to it. `restore_entries` seals values again
under the target's keys with the bundle's token as associated data and the target's fingerprint,
so they open, and erasure by value finds them, as if written there; a value whose fingerprint a
tombstone of the target holds is handled as `vault.erased_values` says (stored erased, refused,
or stored). Erased entries become erased rows. Tokens the target already holds are refused.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import AsyncIterator, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import JsonValue
from sqlalchemy import Table, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.canonical import from_hex
from lorebase_core.canonical import hex as to_hex
from lorebase_core.model import Attribute, looks_like_reference
from lorebase_core.model.vault import TOKEN_PATTERN
from lorebase_core.validate import check_values
from lorebase_store.models import metadata
from lorebase_store.vault.keys import VaultKeys
from lorebase_store.vault.settings import ErasedValues
from lorebase_store.vault.store import Lineage, VaultRow, insert_lineage, insert_rows

_VAULT: Table = metadata.tables["vault"]
_TOMBSTONES: Table = metadata.tables["vault_tombstones"]
_LINEAGE: Table = metadata.tables["vault_lineage"]
_CHUNK = 1000
_FINGERPRINT_BYTES = 32
_ENTRY_FIELDS = frozenset({"token", "kind", "attribute", "record_key", "value", "erased"})
_LINEAGE_FIELDS = frozenset({"kind", "record_key", "previous_key"})
_TOMBSTONE_FIELDS = frozenset({"kind", "attribute", "record_key", "fingerprint", "fingerprint_key"})


class VaultTransferError(ValueError):
    """A vault section cannot be exported, is malformed, or cannot be stored."""


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


@dataclass(frozen=True, slots=True, order=True)
class Holder:
    """A Record holding a token in an Attribute, as a bundle's objects show it."""

    kind_id: str
    attribute_id: str
    record_key: str


@dataclass(frozen=True, slots=True)
class LineageEntry:
    """Record `record_key` took over the vaulted values of `previous_key` (same Kind)."""

    kind_id: str
    record_key: str
    previous_key: str

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind_id,
            "record_key": self.record_key,
            "previous_key": self.previous_key,
        }


@dataclass(frozen=True, slots=True)
class TombstoneEntry:
    """A fingerprint an erasure recorded, of one Record (`record_key`) or of the Kind."""

    kind_id: str
    attribute_id: str
    record_key: str | None
    fingerprint: bytes
    fingerprint_key: str | None

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind_id,
            "attribute": self.attribute_id,
            "record_key": self.record_key,
            "fingerprint": to_hex(self.fingerprint),
            "fingerprint_key": self.fingerprint_key,
        }


class KeyLinks:
    """Record keys of a Kind linked by key changes, transitively in both directions."""

    def __init__(self) -> None:
        self._parent: dict[tuple[str, str], tuple[str, str]] = {}

    def _root(self, node: tuple[str, str]) -> tuple[str, str]:
        while (parent := self._parent.get(node, node)) != node:
            grand = self._parent.get(parent, parent)
            self._parent[node] = grand
            node = grand
        return node

    def link(self, entry: LineageEntry) -> None:
        a = self._root((entry.kind_id, entry.record_key))
        b = self._root((entry.kind_id, entry.previous_key))
        if a != b:
            self._parent[a] = b

    def linked(self, kind_id: str, key: str, other: str) -> bool:
        return key == other or self._root((kind_id, key)) == self._root((kind_id, other))


def holders_problem(
    token: str,
    kind_id: str,
    attribute_id: str,
    record_key: str,
    holders: Collection[Holder],
    links: KeyLinks,
) -> str | None:
    """Why the Records holding `token` do not fit its binding, or None: each must hold it in
    the bound Kind and Attribute, under the bound key or a key linked to it."""
    if not holders:
        return f"no Record object of the bundle holds {token}"
    for holder in sorted(holders):
        if (holder.kind_id, holder.attribute_id) != (kind_id, attribute_id) or not links.linked(
            kind_id, holder.record_key, record_key
        ):
            return (
                f"{token} is held by Record {holder.record_key!r} in Attribute "
                f"{holder.attribute_id}, which is neither its binding nor linked to it"
            )
    return None


async def read_entries(
    session: AsyncSession, domain_id: str, tokens: Sequence[str], keys: VaultKeys
) -> dict[str, VaultEntry]:
    """The entries of the tokens that have a vault row, bound as the row is. A value sealed
    under a key that is not configured cannot be exported."""
    v = _VAULT
    out: dict[str, VaultEntry] = {}
    ordered = sorted(set(tokens))
    for start in range(0, len(ordered), _CHUNK):
        rows = await session.execute(
            select(
                v.c.id,
                v.c.kind_id,
                v.c.attribute_id,
                v.c.record_key,
                v.c.value_ciphertext,
                v.c.key_id,
            ).where(v.c.domain_id == domain_id, v.c.id.in_(ordered[start : start + _CHUNK]))
        )
        for row in rows:
            if row.value_ciphertext is None or row.key_id is None:
                out[row.id] = VaultEntry(
                    row.id, row.kind_id, row.attribute_id, row.record_key, erased=True
                )
                continue
            opened = keys.open(domain_id, row.id, row.key_id, bytes(row.value_ciphertext))
            if opened is None:
                raise VaultTransferError(
                    f"the value of {row.id} is sealed under a key that is not configured "
                    "(vault.previous_keys)"
                )
            out[row.id] = VaultEntry(
                row.id, row.kind_id, row.attribute_id, row.record_key, json.loads(opened)
            )
    return out


def unknown_entry(token: str, holders: Collection[Holder]) -> VaultEntry:
    """The entry of a token without a vault row: erased, bound to its first holder."""
    first = min(holders)
    return VaultEntry(token, first.kind_id, first.attribute_id, first.record_key, erased=True)


async def lineage_of(session: AsyncSession, domain_id: str) -> list[LineageEntry]:
    """The Domain's key changes."""
    lin = _LINEAGE
    rows = await session.execute(
        select(lin.c.kind_id, lin.c.record_key, lin.c.previous_key)
        .where(lin.c.domain_id == domain_id)
        .order_by(lin.c.kind_id, lin.c.record_key, lin.c.previous_key)
    )
    return [LineageEntry(r.kind_id, r.record_key, r.previous_key) for r in rows]


async def tombstones_of(
    session: AsyncSession, domain_id: str, batch: int
) -> AsyncIterator[list[TombstoneEntry]]:
    """The Domain's tombstones, `batch` at a time."""
    t = _TOMBSTONES
    after = 0
    while True:
        rows = (
            await session.execute(
                select(
                    t.c.seq,
                    t.c.kind_id,
                    t.c.attribute_id,
                    t.c.record_key,
                    t.c.fingerprint,
                    t.c.fingerprint_key,
                )
                .where(t.c.domain_id == domain_id, t.c.seq > after)
                .order_by(t.c.seq)
                .limit(batch)
            )
        ).all()
        if not rows:
            return
        yield [
            TombstoneEntry(
                r.kind_id, r.attribute_id, r.record_key, bytes(r.fingerprint), r.fingerprint_key
            )
            for r in rows
        ]
        after = int(rows[-1].seq)


def _strings(line: Mapping[str, Any], names: Sequence[str], label: str) -> list[str]:
    found = [line.get(name) for name in names]
    if not all(isinstance(part, str) and part for part in found):
        raise VaultTransferError(f"{label}: {', '.join(names)} must be non-empty strings")
    return [str(part) for part in found]


def _fields(line: Mapping[str, Any], allowed: frozenset[str]) -> None:
    unknown = set(line) - allowed
    if unknown:
        raise VaultTransferError(f"unexpected fields {sorted(unknown)}")


def parse_entry(line: Mapping[str, Any]) -> VaultEntry:
    """A checked entry: a well-formed token, its binding as strings, and exactly one of a
    value (never a reference) or the erased mark."""
    _fields(line, _ENTRY_FIELDS)
    token = line.get("token")
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise VaultTransferError(f"malformed token {token!r}")
    kind_id, attribute_id, record_key = _strings(line, ("kind", "attribute", "record_key"), token)
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


def parse_lineage(line: Mapping[str, Any]) -> LineageEntry:
    """A checked key change."""
    _fields(line, _LINEAGE_FIELDS)
    kind_id, record_key, previous_key = _strings(
        line, ("kind", "record_key", "previous_key"), "key change"
    )
    if record_key == previous_key:
        raise VaultTransferError(f"key change of {record_key!r} to itself")
    return LineageEntry(kind_id, record_key, previous_key)


def parse_tombstone(line: Mapping[str, Any], token_key_ids: Collection[str]) -> TombstoneEntry:
    """A checked tombstone, made under one of `token_key_ids` (the target's token keys)."""
    _fields(line, _TOMBSTONE_FIELDS)
    kind_id, attribute_id = _strings(line, ("kind", "attribute"), "tombstone")
    record_key = line.get("record_key")
    if record_key is not None and (not isinstance(record_key, str) or not record_key):
        raise VaultTransferError("tombstone: record_key must be a non-empty string or null")
    raw = line.get("fingerprint")
    try:
        fingerprint = from_hex(raw) if isinstance(raw, str) else b""
    except ValueError:
        fingerprint = b""
    if len(fingerprint) != _FINGERPRINT_BYTES:
        raise VaultTransferError(f"tombstone: malformed fingerprint {raw!r}")
    fingerprint_key = line.get("fingerprint_key")
    key_id = fingerprint_key.partition("/")[0] if isinstance(fingerprint_key, str) else None
    if key_id is None or "/" not in str(fingerprint_key) or key_id not in token_key_ids:
        raise VaultTransferError(
            f"a tombstone was made under token key {fingerprint_key!r}, which this "
            "installation does not have (vault.token_key_id, vault.previous_token_keys); "
            "configure that token key under the same id to import the bundle"
        )
    return TombstoneEntry(kind_id, attribute_id, record_key, fingerprint, fingerprint_key)


def value_problem(entry: VaultEntry, definitions: Sequence[Attribute]) -> str | None:
    """Why the value of `entry` fits none of the definitions its Attribute had in the bundle's
    schemas (type, canonical form, constraints), or None."""
    if not definitions:
        return f"{entry.token}: the bundle's schemas have no vault Attribute {entry.attribute_id}"
    issues: list[str] = []
    for attribute in definitions:
        found = check_values(
            {entry.attribute_id: attribute},
            {entry.attribute_id: entry.value},
            ("vault",),
            kind=entry.kind_id,
            key=entry.record_key,
        )
        if not found:
            return None
        issues.append(found[0].message)
    return f"{entry.token}: {issues[0]}"


async def existing_tokens(
    session: AsyncSession, domain_id: str, tokens: Collection[str]
) -> set[str]:
    """The tokens among `tokens` that the Domain's vault already holds."""
    v = _VAULT
    ordered = sorted(tokens)
    found: set[str] = set()
    for start in range(0, len(ordered), _CHUNK):
        rows = await session.execute(
            select(v.c.id).where(
                v.c.domain_id == domain_id, v.c.id.in_(ordered[start : start + _CHUNK])
            )
        )
        found.update(rows.scalars())
    return found


async def restore_lineage(
    session: AsyncSession, domain_id: str, entries: Sequence[LineageEntry], *, at: datetime
) -> None:
    """Store key changes."""
    for start in range(0, len(entries), _CHUNK):
        chunk = entries[start : start + _CHUNK]
        await insert_lineage(
            session,
            domain_id,
            [Lineage(e.kind_id, e.record_key, e.previous_key) for e in chunk],
            at=at,
        )


async def restore_tombstones(
    session: AsyncSession, domain_id: str, entries: Sequence[TombstoneEntry], *, at: datetime
) -> None:
    """Store tombstones as they were made."""
    if not entries:
        return
    await session.execute(
        insert(_TOMBSTONES).values(
            [
                {
                    "domain_id": domain_id,
                    "kind_id": e.kind_id,
                    "attribute_id": e.attribute_id,
                    "record_key": e.record_key,
                    "fingerprint": e.fingerprint,
                    "fingerprint_key": e.fingerprint_key,
                    "created_at": at,
                }
                for e in entries
            ]
        )
    )


async def _tombstoned(
    session: AsyncSession, domain_id: str, values: Sequence[VaultEntry], keys: VaultKeys
) -> set[str]:
    """Tokens of the values a tombstone of the Domain holds, for their Record or the Kind."""
    fingerprints = {
        e.token: keys.fingerprints(domain_id, e.kind_id, e.attribute_id, e.value) for e in values
    }
    wanted = sorted({f for found in fingerprints.values() for f in found})
    t = _TOMBSTONES
    erased: dict[bytes, list[tuple[str, str, str | None]]] = defaultdict(list)
    for start in range(0, len(wanted), _CHUNK):
        rows = await session.execute(
            select(t.c.fingerprint, t.c.kind_id, t.c.attribute_id, t.c.record_key).where(
                t.c.domain_id == domain_id, t.c.fingerprint.in_(wanted[start : start + _CHUNK])
            )
        )
        for row in rows:
            erased[bytes(row.fingerprint)].append((row.kind_id, row.attribute_id, row.record_key))
    if not erased:
        return set()
    return {
        e.token
        for e in values
        if any(
            (kind_id, attribute_id) == (e.kind_id, e.attribute_id)
            and record_key in (None, e.record_key)
            for fingerprint in fingerprints[e.token]
            for kind_id, attribute_id, record_key in erased.get(fingerprint, ())
        )
    }


async def restore_entries(
    session: AsyncSession,
    domain_id: str,
    entries: Sequence[VaultEntry],
    keys: VaultKeys | None,
    *,
    erased_values: ErasedValues,
    at: datetime,
) -> int:
    """Store `entries` in the Domain's vault (module doc); the number of values stored erased
    because a tombstone holds them."""
    if not entries:
        return 0
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
    tombstoned: set[str] = set()
    if values and erased_values is not ErasedValues.ALLOW:
        assert keys is not None
        tombstoned = await _tombstoned(session, domain_id, values, keys)
        if tombstoned and erased_values is ErasedValues.REFUSE:
            raise VaultTransferError(
                f"{len(tombstoned)} values of the bundle were erased, e.g. {min(tombstoned)} "
                "(vault.erased_values: refuse)"
            )
    rows: list[VaultRow] = []
    for e in entries:
        if e.erased or e.token in tombstoned:
            rows.append(
                VaultRow(
                    e.token, e.kind_id, e.attribute_id, e.record_key, None, None, None, erased=True
                )
            )
            continue
        assert keys is not None
        rows.append(
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
        )
    await insert_rows(session, domain_id, rows, at=at)
    return len(tombstoned)
