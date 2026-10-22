"""The `vault` table: storing values behind tokens, opening them, erasing them (design 13.5).

- `tokenize` runs in the commit pipeline (and wherever Records are written outside it): the
  plain values of vault Attributes in Record upserts become references, and the rows to insert
  are returned. A reference already in an upsert is accepted when it is the value the Record
  holds at the branch head, or when a vault row binds it to the same Kind, Record key and
  Attribute; any other reference is refused, so a writer cannot copy another Record's value.
- `open_values` reads rows by token: the value, `erased`, or `unavailable` (the key that
  sealed it is not configured).
- `erase_record` and `erase_matching` erase rows: the ciphertext, key id and fingerprint are
  cleared and `erased_at` is set. The row stays, so the token keeps answering `erased` and a
  later write of the same value gets a new token (`lorebase_store.vault.keys`). Erasing an
  erased row changes nothing.

Every statement filters on `domain_id` and runs under the Domain's row-level security scope.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import JsonValue
from sqlalchemy import Table, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from lorebase_core.model import Record, Schema, looks_like_reference, vault_reference, vault_token
from lorebase_core.validate import DataChange, Issue, IssueCode, RecordUpsert, check_values
from lorebase_store.models import metadata
from lorebase_store.vault.keys import VaultKeys

VAULT: Table = metadata.tables["vault"]

MAX_GENERATIONS = 64
"""Erased generations of one value of one Record tried before a write is refused."""


class RecordValues(Protocol):
    """The values a Record holds at the branch head (`MaterializedIndexView`)."""

    def record_state(self, kind_id: str, key: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class VaultRow:
    """A row to insert."""

    token: str
    kind_id: str
    attribute_id: str
    record_key: str
    fingerprint: bytes
    ciphertext: bytes
    key_id: str


@dataclass(frozen=True, slots=True)
class Tokenized:
    changes: list[DataChange]
    rows: list[VaultRow]
    issues: list[Issue]


@dataclass(frozen=True, slots=True)
class _Binding:
    kind_id: str
    attribute_id: str
    record_key: str
    erased: bool


@dataclass(frozen=True, slots=True)
class _Plain:
    index: int
    kind_id: str
    key: str
    attribute_id: str
    value: JsonValue


def vault_attributes(schema: Schema) -> dict[str, list[str]]:
    """Kind id -> ids of its vault Attributes."""
    found: dict[str, list[str]] = {}
    for kind in schema.kinds.values():
        ids = [a.id for a in kind.attributes.values() if a.in_vault]
        if ids:
            found[kind.id] = ids
    return found


def tokenize(
    sync: Session,
    domain_id: str,
    schema: Schema,
    changes: Sequence[DataChange],
    keys: VaultKeys | None,
    current: RecordValues,
) -> Tokenized:
    """Replace the plain values of vault Attributes in `changes` by references (module doc).

    Plain values are validated first (type, canonical form, constraints), as the core rules
    only see the reference afterwards.
    """
    vaulted = vault_attributes(schema)
    if not vaulted:
        return Tokenized(list(changes), [], [])
    plain: list[_Plain] = []
    claimed: list[tuple[int, str, str, str, str]] = []
    issues: list[Issue] = []
    for index, upsert in enumerate(changes):
        if not isinstance(upsert, RecordUpsert) or upsert.record.kind not in vaulted:
            continue
        record = upsert.record
        kind = schema.kinds[record.kind]
        held = None
        for attribute_id in vaulted[record.kind]:
            value = record.values.get(attribute_id)
            if value is None:
                continue
            if looks_like_reference(value):
                token = vault_token(value)
                if token is None:
                    continue  # malformed: the core rules refuse it
                if held is None:
                    state = current.record_state(record.kind, record.key)
                    held = {} if state is None else state.values
                if held.get(attribute_id) != value:
                    claimed.append((index, record.kind, record.key, attribute_id, token))
                continue
            attribute = kind.attributes[attribute_id]
            found = check_values(
                {attribute_id: attribute},
                {attribute_id: value},
                ("changes", index, "record"),
                kind=record.kind,
                key=record.key,
            )
            if found:
                issues.extend(found)
            else:
                plain.append(_Plain(index, record.kind, record.key, attribute_id, value))
    issues.extend(_check_claims(sync, domain_id, claimed))
    if plain and keys is None:
        issues.extend(
            Issue(
                code=IssueCode.VALUE_VAULT_UNAVAILABLE,
                path=("changes", item.index, "record", "values", item.attribute_id),
                message="the vault is not configured (vault.token_key, vault.encryption_key)",
                kind=item.kind_id,
                key=item.key,
                attribute=item.attribute_id,
            )
            for item in plain
        )
    if issues:
        return Tokenized(list(changes), [], issues)
    assert keys is not None or not plain
    tokens, rows, exhausted = _mint(sync, domain_id, plain, keys) if keys else ({}, [], [])
    issues.extend(
        Issue(
            code=IssueCode.VALUE_VAULT_UNAVAILABLE,
            path=("changes", item.index, "record", "values", item.attribute_id),
            message=f"this value was erased {MAX_GENERATIONS} times and cannot be stored again",
            kind=item.kind_id,
            key=item.key,
            attribute=item.attribute_id,
        )
        for item in exhausted
    )
    if issues:
        return Tokenized(list(changes), [], issues)
    replaced: dict[int, dict[str, JsonValue]] = defaultdict(dict)
    for item in plain:
        replaced[item.index][item.attribute_id] = vault_reference(tokens[item])
    out = list(changes)
    for index, values in replaced.items():
        upsert = out[index]
        assert isinstance(upsert, RecordUpsert)
        updated: Record = upsert.record.model_copy(
            update={"values": {**upsert.record.values, **values}}
        )
        out[index] = RecordUpsert(record=updated)
    return Tokenized(out, rows, [])


def _check_claims(
    sync: Session, domain_id: str, claimed: Sequence[tuple[int, str, str, str, str]]
) -> list[Issue]:
    """Issues for references that no vault row binds to the Record and Attribute claiming
    them."""
    if not claimed:
        return []
    bindings = _bindings(sync, domain_id, {token for *_, token in claimed})
    issues: list[Issue] = []
    for index, kind_id, key, attribute_id, token in claimed:
        found = bindings.get(token)
        if found is not None and (found.kind_id, found.record_key, found.attribute_id) == (
            kind_id,
            key,
            attribute_id,
        ):
            continue
        issues.append(
            Issue(
                code=IssueCode.VALUE_VAULT_REFERENCE,
                path=("changes", index, "record", "values", attribute_id),
                message="the vault reference does not belong to this Record and Attribute",
                kind=kind_id,
                key=key,
                attribute=attribute_id,
            )
        )
    return issues


def _bindings(sync: Session, domain_id: str, tokens: Collection[str]) -> dict[str, _Binding]:
    v = VAULT
    found: dict[str, _Binding] = {}
    ordered = sorted(tokens)
    for start in range(0, len(ordered), 1000):
        rows = sync.execute(
            select(v.c.id, v.c.kind_id, v.c.attribute_id, v.c.record_key, v.c.erased_at).where(
                v.c.domain_id == domain_id, v.c.id.in_(ordered[start : start + 1000])
            )
        )
        for row in rows:
            found[row.id] = _Binding(
                row.kind_id, row.attribute_id, row.record_key, row.erased_at is not None
            )
    return found


def _mint(
    sync: Session, domain_id: str, plain: Sequence[_Plain], keys: VaultKeys
) -> tuple[dict[_Plain, str], list[VaultRow], list[_Plain]]:
    """Token of every plain value, the rows to insert and the values whose generations ran
    out. A token whose row exists and is not erased is reused as it is."""
    tokens: dict[_Plain, str] = {}
    rows: dict[str, VaultRow] = {}
    exhausted: list[_Plain] = []
    pending = dict.fromkeys(plain, 0)
    while pending:
        candidates = {
            item: keys.token(
                domain_id, item.kind_id, item.key, item.attribute_id, item.value, generation
            )
            for item, generation in pending.items()
        }
        existing = _bindings(sync, domain_id, set(candidates.values()))
        retry: dict[_Plain, int] = {}
        for item, token in candidates.items():
            found = existing.get(token)
            if found is not None and found.erased:
                generation = pending[item] + 1
                if generation >= MAX_GENERATIONS:
                    exhausted.append(item)
                else:
                    retry[item] = generation
                continue
            tokens[item] = token
            if found is None and token not in rows:
                rows[token] = VaultRow(
                    token=token,
                    kind_id=item.kind_id,
                    attribute_id=item.attribute_id,
                    record_key=item.key,
                    fingerprint=keys.fingerprint(
                        domain_id, item.kind_id, item.attribute_id, item.value
                    ),
                    ciphertext=keys.seal(domain_id, token, item.value),
                    key_id=keys.key_id,
                )
        pending = retry
    return tokens, list(rows.values()), exhausted


async def insert_rows(
    session: AsyncSession, domain_id: str, rows: Sequence[VaultRow], *, at: datetime
) -> None:
    """Insert rows minted by `tokenize`; a token stored meanwhile is kept as it is."""
    if not rows:
        return
    for start in range(0, len(rows), 500):
        chunk = rows[start : start + 500]
        await session.execute(
            insert(VAULT)
            .values(
                [
                    {
                        "domain_id": domain_id,
                        "id": row.token,
                        "kind_id": row.kind_id,
                        "attribute_id": row.attribute_id,
                        "record_key": row.record_key,
                        "fingerprint": row.fingerprint,
                        "value_ciphertext": row.ciphertext,
                        "key_id": row.key_id,
                        "created_at": at,
                    }
                    for row in chunk
                ]
            )
            .on_conflict_do_nothing(index_elements=["domain_id", "id"])
        )


class VaultState(StrEnum):
    VALUE = "value"
    ERASED = "erased"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class VaultValue:
    """What a token stands for. `kind_id`/`attribute_id` are None for an unknown token (from a
    bundle of another installation, say), which reads as erased."""

    state: VaultState
    kind_id: str | None = None
    attribute_id: str | None = None
    value: JsonValue = None


async def open_values(
    session: AsyncSession, domain_id: str, tokens: Iterable[str], keys: VaultKeys | None
) -> dict[str, VaultValue]:
    """The value of each token (see `VaultValue`)."""
    wanted = sorted(set(tokens))
    out: dict[str, VaultValue] = {t: VaultValue(VaultState.ERASED) for t in wanted}
    v = VAULT
    for start in range(0, len(wanted), 1000):
        rows = await session.execute(
            select(v.c.id, v.c.kind_id, v.c.attribute_id, v.c.value_ciphertext, v.c.key_id).where(
                v.c.domain_id == domain_id, v.c.id.in_(wanted[start : start + 1000])
            )
        )
        for row in rows:
            if row.value_ciphertext is None or row.key_id is None:
                out[row.id] = VaultValue(VaultState.ERASED, row.kind_id, row.attribute_id)
                continue
            opened = (
                None
                if keys is None
                else keys.open(domain_id, row.id, row.key_id, bytes(row.value_ciphertext))
            )
            if opened is None:
                out[row.id] = VaultValue(VaultState.UNAVAILABLE, row.kind_id, row.attribute_id)
            else:
                out[row.id] = VaultValue(
                    VaultState.VALUE, row.kind_id, row.attribute_id, json.loads(opened)
                )
    return out


async def erase_record(
    session: AsyncSession,
    domain_id: str,
    kind_id: str,
    key: str,
    attribute_ids: Collection[str] | None = None,
    *,
    at: datetime | None = None,
) -> int:
    """Erase every vaulted value a Record ever held (of `attribute_ids` only, when given), on
    every branch and Revision; the number of rows erased now."""
    v = VAULT
    condition = [v.c.domain_id == domain_id, v.c.kind_id == kind_id, v.c.record_key == key]
    if attribute_ids is not None:
        condition.append(v.c.attribute_id.in_(sorted(attribute_ids)))
    return await _erase(session, condition, at)


async def erase_matching(
    session: AsyncSession,
    domain_id: str,
    kind_id: str,
    attribute_id: str,
    fingerprint: bytes,
    *,
    at: datetime | None = None,
) -> int:
    """Erase the value with this fingerprint in every Record of the Kind; the rows erased."""
    v = VAULT
    condition = [
        v.c.domain_id == domain_id,
        v.c.kind_id == kind_id,
        v.c.attribute_id == attribute_id,
        v.c.fingerprint == fingerprint,
    ]
    return await _erase(session, condition, at)


async def matching_keys(
    session: AsyncSession, domain_id: str, kind_id: str, attribute_id: str, fingerprint: bytes
) -> list[str]:
    """Keys of the Records whose live vault rows hold the value with this fingerprint."""
    v = VAULT
    rows = await session.execute(
        select(v.c.record_key)
        .where(
            v.c.domain_id == domain_id,
            v.c.kind_id == kind_id,
            v.c.attribute_id == attribute_id,
            v.c.fingerprint == fingerprint,
        )
        .distinct()
        .order_by(v.c.record_key)
    )
    return list(rows.scalars())


async def _erase(session: AsyncSession, condition: list[Any], at: datetime | None) -> int:
    v = VAULT
    result = await session.execute(
        update(v)
        .where(*condition, v.c.erased_at.is_(None))
        .values(
            value_ciphertext=None,
            key_id=None,
            fingerprint=None,
            erased_at=at or func.now(),
        )
        .returning(v.c.id)
    )
    return len(result.all())
