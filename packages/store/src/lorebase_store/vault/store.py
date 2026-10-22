"""The `vault` table: storing values behind tokens, opening them, erasing them (design 13.5).

- `tokenize` runs in the commit pipeline: the plain values of vault Attributes in Record
  upserts become references, and the rows to insert are returned. A reference already in an
  upsert is accepted when it is the value the Record holds at the branch head, or when a vault
  row binds it to the same Kind, Record key and Attribute. A reference another Record of the
  Kind holds in the same Attribute is taken over when the same change set deletes that Record
  and the taking Record does not exist at the head (a key change), once per reference: its
  value is stored again under a token of the new key, and `vault_lineage` records that both
  keys hold the same data subject's values. Any other reference is refused, so a writer cannot
  copy another Record's value.
- In commits of the origins of `vault.tombstone_origins` (Source syncs, extraction, imports),
  a plain value whose fingerprint an erasure recorded (`vault_tombstones`) is stored erased,
  refused, or stored again, as `vault.erased_values` says. Other writers are people and agents
  who choose the value: checking their writes would tell them whether a guessed value was
  erased.
- `open_values` reads rows by token: the value, `erased`, or `unavailable` (the key that
  sealed it is not configured).
- `erase_record` and `erase_matching` erase rows: the ciphertext, key id and fingerprint are
  cleared and `erased_at` is set. The row stays, so the token keeps answering `erased`; the
  fingerprints of the erased values go to `vault_tombstones`. Erasing an erased row changes
  nothing. Erasure by record follows `vault_lineage` to every key the Record had; erasure by
  value compares matching forms (`lorebase_core.types.matching`), so case variants of the value
  are erased too.

Commits that store vault values hold the Domain's vault lock shared from the first vault read to
the end of their transaction, erasures hold it exclusively: an erasure waits for the commits in
flight, and a commit that starts during an erasure reads the vault after it. A value is thus
either written before the erasure (and erased by it) or after it (and finds its tombstone).

Every statement filters on `domain_id` and runs under the Domain's row-level security scope.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import JsonValue
from sqlalchemy import Table, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from lorebase_core.model import Record, Schema, looks_like_reference, vault_reference, vault_token
from lorebase_core.validate import (
    DataChange,
    Issue,
    IssueCode,
    RecordDelete,
    RecordUpsert,
    check_values,
)
from lorebase_store.models import metadata
from lorebase_store.repositories.refs import LOCK_NOT_AVAILABLE, sqlstate
from lorebase_store.vault.keys import VaultKeys
from lorebase_store.vault.settings import ErasedValues

VAULT: Table = metadata.tables["vault"]
TOMBSTONES: Table = metadata.tables["vault_tombstones"]
LINEAGE: Table = metadata.tables["vault_lineage"]

MAX_GENERATIONS = 64
"""Erased generations of one value of one Record tried before a write is refused."""

MAX_LINKED_KEYS = 1000
"""Keys of one data subject above which an erasure by key is flagged for an administrator
(`Erasure.linked`); it still erases every one of them."""


class VaultLockTimeoutError(Exception):
    """The Domain's vault lock was not acquired within the configured time."""


_CHUNK = 1000

_LOCK = "SELECT {function}(hashtextextended('lorebase.vault/' || :domain, 0))"


class RecordValues(Protocol):
    """The values a Record holds at the branch head (`MaterializedIndexView`)."""

    def record_state(self, kind_id: str, key: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class VaultRow:
    """A row to insert. An erased row (a write of an erased value, `vault.erased_values:
    erase`) holds no ciphertext, key id or fingerprint."""

    token: str
    kind_id: str
    attribute_id: str
    record_key: str
    fingerprint: bytes | None
    ciphertext: bytes | None
    key_id: str | None
    fingerprint_key: str | None = None
    erased: bool = False


@dataclass(frozen=True, slots=True)
class Lineage:
    """Record `record_key` took over vaulted values of `previous_key` (same Kind)."""

    kind_id: str
    record_key: str
    previous_key: str


@dataclass(frozen=True, slots=True)
class Tokenized:
    changes: list[DataChange]
    rows: list[VaultRow]
    issues: list[Issue]
    lineage: list[Lineage] = field(default_factory=list)
    moved: list[tuple[str, str]] = field(default_factory=list)
    """Tokens a key change took over, with the key their value moved to (`mark_moved`)."""


@dataclass(frozen=True, slots=True)
class _Binding:
    kind_id: str
    attribute_id: str
    record_key: str
    erased: bool
    moved_to: str | None = None


@dataclass(frozen=True, slots=True)
class _Plain:
    index: int
    kind_id: str
    key: str
    attribute_id: str
    value: JsonValue


@dataclass(frozen=True, slots=True)
class _Claim:
    index: int
    kind_id: str
    key: str
    attribute_id: str
    token: str


def vault_attributes(schema: Schema) -> dict[str, list[str]]:
    """Kind id -> ids of its vault Attributes."""
    found: dict[str, list[str]] = {}
    for kind in schema.kinds.values():
        ids = [a.id for a in kind.attributes.values() if a.in_vault]
        if ids:
            found[kind.id] = ids
    return found


def lock_vault(sync: Session, domain_id: str, *, timeout_ms: int = 0) -> None:
    """Hold the Domain's vault lock shared until the transaction ends (module doc), waiting
    at most `timeout_ms` (0: no limit); raises `VaultLockTimeoutError`, after which the
    transaction must be rolled back."""
    statement = text(_LOCK.format(function="pg_advisory_xact_lock_shared"))
    if timeout_ms <= 0:
        sync.execute(statement, {"domain": domain_id})
        return
    previous = sync.scalar(text("SELECT current_setting('lock_timeout')"))
    sync.execute(text("SELECT set_config('lock_timeout', :t, true)"), {"t": f"{timeout_ms}ms"})
    try:
        sync.execute(statement, {"domain": domain_id})
    except DBAPIError as error:
        if sqlstate(error) == LOCK_NOT_AVAILABLE:
            raise VaultLockTimeoutError(
                f"the vault of Domain {domain_id!r} stayed locked for {timeout_ms} ms"
            ) from error
        raise
    sync.execute(text("SELECT set_config('lock_timeout', :t, true)"), {"t": str(previous)})


async def lock_vault_exclusive(
    session: AsyncSession, domain_id: str, *, timeout_ms: int = 0
) -> None:
    """Hold the Domain's vault lock exclusively until the transaction ends (module doc),
    waiting at most `timeout_ms` (0: no limit); raises `VaultLockTimeoutError`."""
    statement = text(_LOCK.format(function="pg_advisory_xact_lock"))
    if timeout_ms <= 0:
        await session.execute(statement, {"domain": domain_id})
        return
    previous = await session.scalar(text("SELECT current_setting('lock_timeout')"))
    await session.execute(
        text("SELECT set_config('lock_timeout', :t, true)"), {"t": f"{timeout_ms}ms"}
    )
    try:
        await session.execute(statement, {"domain": domain_id})
    except DBAPIError as error:
        if sqlstate(error) == LOCK_NOT_AVAILABLE:
            raise VaultLockTimeoutError(
                f"the vault of Domain {domain_id!r} stayed locked for {timeout_ms} ms"
            ) from error
        raise
    await session.execute(text("SELECT set_config('lock_timeout', :t, true)"), {"t": str(previous)})


def tokenize(
    sync: Session,
    domain_id: str,
    schema: Schema,
    changes: Sequence[DataChange],
    keys: VaultKeys | None,
    current: RecordValues,
    *,
    erased_values: ErasedValues = ErasedValues.ERASE,
    tombstones: bool = True,
    lock_timeout_ms: int = 0,
) -> Tokenized:
    """Replace the plain values of vault Attributes in `changes` by references (module doc).

    Plain values are validated first (type, canonical form, constraints), as the core rules
    only see the reference afterwards.
    """
    vaulted = vault_attributes(schema)
    if not vaulted:
        return Tokenized(list(changes), [], [])
    deleted = {(change.kind, change.key) for change in changes if isinstance(change, RecordDelete)}
    plain: list[_Plain] = []
    claimed: list[_Claim] = []
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
                    claimed.append(_Claim(index, record.kind, record.key, attribute_id, token))
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
    if not plain and not claimed:
        return Tokenized(list(changes), [], issues)
    lock_vault(sync, domain_id, timeout_ms=lock_timeout_ms)
    taken, lineage, moved, claim_issues = _check_claims(
        sync, domain_id, claimed, deleted, current, keys
    )
    issues.extend(claim_issues)
    plain.extend(taken)
    if plain and keys is None:
        issues.extend(
            _issue(
                IssueCode.VALUE_VAULT_UNAVAILABLE,
                item,
                "the vault is not configured (vault.token_key, vault.encryption_key)",
            )
            for item in plain
        )
    if issues:
        return Tokenized(list(changes), [], issues)
    erased: set[_Plain] = set()
    if keys is not None and plain and tombstones and erased_values is not ErasedValues.ALLOW:
        erased = _tombstoned(sync, domain_id, plain, keys)
        if erased and erased_values is ErasedValues.REFUSE:
            return Tokenized(
                list(changes),
                [],
                [
                    _issue(IssueCode.VALUE_VAULT_ERASED, item, "this value was erased")
                    for item in plain
                    if item in erased
                ],
            )
    assert keys is not None or not plain
    tokens, rows, exhausted = _mint(sync, domain_id, plain, keys, erased) if keys else ({}, [], [])
    issues.extend(
        _issue(
            IssueCode.VALUE_VAULT_UNAVAILABLE,
            item,
            f"this value was erased {MAX_GENERATIONS} times and cannot be stored again",
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
    return Tokenized(out, rows, [], lineage, moved)


def _issue(code: IssueCode, item: _Plain | _Claim, message: str) -> Issue:
    return Issue(
        code=code,
        path=("changes", item.index, "record", "values", item.attribute_id),
        message=message,
        kind=item.kind_id,
        key=item.key,
        attribute=item.attribute_id,
    )


def _check_claims(
    sync: Session,
    domain_id: str,
    claimed: Sequence[_Claim],
    deleted: Collection[tuple[str, str]],
    current: RecordValues,
    keys: VaultKeys | None,
) -> tuple[list[_Plain], list[Lineage], list[tuple[str, str]], list[Issue]]:
    """References no vault row binds to the Record and Attribute claiming them: the values of
    key changes to store again under the new key, the lineage they make, the tokens they move
    and issues for the others. A token moved once (`moved_to`) is taken over by no other key
    change and claimed again by no Record; its rows are locked, so two commits cannot move it
    at the same time."""
    if not claimed:
        return [], [], [], []
    bindings = _bindings(sync, domain_id, {claim.token for claim in claimed}, for_update=True)
    claims_per_token = Counter(claim.token for claim in claimed)
    taken: list[tuple[_Claim, str]] = []
    lineage: dict[tuple[str, str, str], Lineage] = {}
    moved: dict[str, str] = {}
    issues: list[Issue] = []
    for claim in claimed:
        found = bindings.get(claim.token)
        if found is not None and (found.kind_id, found.attribute_id) == (
            claim.kind_id,
            claim.attribute_id,
        ):
            if found.record_key == claim.key and found.moved_to is None:
                continue
            if (
                found.record_key != claim.key
                and found.moved_to is None
                and claims_per_token[claim.token] == 1
                and _moved(claim, found, deleted, current)
            ):
                lineage[(claim.kind_id, claim.key, found.record_key)] = Lineage(
                    claim.kind_id, claim.key, found.record_key
                )
                moved[claim.token] = claim.key
                if not found.erased:
                    taken.append((claim, found.record_key))
                continue  # an erased value moves as the erased reference it is
        issues.append(
            _issue(
                IssueCode.VALUE_VAULT_REFERENCE,
                claim,
                "the vault reference does not belong to this Record and Attribute",
            )
        )
    if issues or not taken:
        return [], list(lineage.values()), sorted(moved.items()), issues
    opened = _open_sync(sync, domain_id, [claim.token for claim, _ in taken], keys)
    plain: list[_Plain] = []
    for claim, _ in taken:
        value = opened.get(claim.token)
        if value is None:
            issues.append(
                _issue(
                    IssueCode.VALUE_VAULT_UNAVAILABLE,
                    claim,
                    "the value of the vault reference cannot be opened with the configured keys",
                )
            )
        else:
            plain.append(_Plain(claim.index, claim.kind_id, claim.key, claim.attribute_id, value))
    return plain, list(lineage.values()), sorted(moved.items()), issues


def _moved(
    claim: _Claim,
    found: _Binding,
    deleted: Collection[tuple[str, str]],
    current: RecordValues,
) -> bool:
    """Whether the claim takes over the reference from a Record the same change set deletes,
    which holds it in that Attribute at the branch head, for a Record that does not exist
    there yet."""
    if (claim.kind_id, found.record_key) not in deleted:
        return False
    if current.record_state(claim.kind_id, claim.key) is not None:
        return False
    state = current.record_state(claim.kind_id, found.record_key)
    held = None if state is None else state.values.get(claim.attribute_id)
    return vault_token(held) == claim.token


def _open_sync(
    sync: Session, domain_id: str, tokens: Sequence[str], keys: VaultKeys | None
) -> dict[str, JsonValue]:
    """The values of live rows that open with `keys`."""
    if keys is None:
        return {}
    v = VAULT
    out: dict[str, JsonValue] = {}
    ordered = sorted(set(tokens))
    for start in range(0, len(ordered), _CHUNK):
        rows = sync.execute(
            select(v.c.id, v.c.value_ciphertext, v.c.key_id).where(
                v.c.domain_id == domain_id,
                v.c.id.in_(ordered[start : start + _CHUNK]),
                v.c.value_ciphertext.is_not(None),
            )
        )
        for row in rows:
            opened = keys.open(domain_id, row.id, row.key_id, bytes(row.value_ciphertext))
            if opened is not None:
                out[row.id] = json.loads(opened)
    return out


def _tombstoned(
    sync: Session, domain_id: str, plain: Sequence[_Plain], keys: VaultKeys
) -> set[_Plain]:
    """The plain values an erasure recorded, for their Record or for every Record of the
    Kind."""
    fingerprints = {
        item: keys.fingerprints(domain_id, item.kind_id, item.attribute_id, item.value)
        for item in plain
    }
    wanted = sorted({f for found in fingerprints.values() for f in found})
    t = TOMBSTONES
    erased: dict[bytes, list[tuple[str, str, str | None]]] = defaultdict(list)
    for start in range(0, len(wanted), _CHUNK):
        rows = sync.execute(
            select(t.c.fingerprint, t.c.kind_id, t.c.attribute_id, t.c.record_key).where(
                t.c.domain_id == domain_id, t.c.fingerprint.in_(wanted[start : start + _CHUNK])
            )
        )
        for row in rows:
            erased[bytes(row.fingerprint)].append((row.kind_id, row.attribute_id, row.record_key))
    if not erased:
        return set()
    return {
        item
        for item, found in fingerprints.items()
        if any(
            kind_id == item.kind_id
            and attribute_id == item.attribute_id
            and record_key in (None, item.key)
            for fingerprint in found
            for kind_id, attribute_id, record_key in erased.get(fingerprint, ())
        )
    }


def _bindings(
    sync: Session, domain_id: str, tokens: Collection[str], *, for_update: bool = False
) -> dict[str, _Binding]:
    v = VAULT
    found: dict[str, _Binding] = {}
    ordered = sorted(tokens)
    for start in range(0, len(ordered), _CHUNK):
        query = (
            select(
                v.c.id, v.c.kind_id, v.c.attribute_id, v.c.record_key, v.c.erased_at, v.c.moved_to
            )
            .where(v.c.domain_id == domain_id, v.c.id.in_(ordered[start : start + _CHUNK]))
            .order_by(v.c.id)
        )
        if for_update:
            query = query.with_for_update()
        for row in sync.execute(query):
            found[row.id] = _Binding(
                row.kind_id,
                row.attribute_id,
                row.record_key,
                row.erased_at is not None,
                row.moved_to,
            )
    return found


@dataclass(slots=True)
class _Search:
    """Where the generation search of one value stands."""

    generation: int = 0
    free: int | None = None
    """Lowest generation whose token under the current key has no row."""
    erased: str | None = None
    """An erased token of the value (any key), reused by a write of an erased value."""


def _mint(
    sync: Session,
    domain_id: str,
    plain: Sequence[_Plain],
    keys: VaultKeys,
    erase: Collection[_Plain],
) -> tuple[dict[_Plain, str], list[VaultRow], list[_Plain]]:
    """Token of every plain value, the rows to insert and the values whose generations ran
    out.

    Generation by generation, the value's token under each configured token key is looked up:
    a live row (under any key) is reused as it is. Rows of a key exist for consecutive
    generations from 0, so a generation without a row under any key ends the search; the value
    then gets the token of the lowest free generation under the current key. A value an erasure
    recorded (`erase`) never reuses a live row: it reuses an erased token of its own, or gets a
    new erased row.
    """
    tokens: dict[_Plain, str] = {}
    rows: dict[str, VaultRow] = {}
    exhausted: list[_Plain] = []
    pending = {item: _Search() for item in plain}

    def mint(item: _Plain, search: _Search) -> None:
        if item in erase and search.erased is not None:
            tokens[item] = search.erased
            return
        if search.free is None:
            exhausted.append(item)
            return
        token = keys.token(
            domain_id, item.kind_id, item.key, item.attribute_id, item.value, search.free
        )
        tokens[item] = token
        if token in rows:
            return
        if item in erase:
            rows[token] = VaultRow(
                token, item.kind_id, item.attribute_id, item.key, None, None, None, erased=True
            )
            return
        rows[token] = VaultRow(
            token=token,
            kind_id=item.kind_id,
            attribute_id=item.attribute_id,
            record_key=item.key,
            fingerprint=keys.fingerprint(domain_id, item.kind_id, item.attribute_id, item.value),
            ciphertext=keys.seal(domain_id, token, item.value),
            key_id=keys.key_id,
            fingerprint_key=keys.fingerprint_key,
        )

    while pending:
        candidates = {
            item: [
                keys.token(
                    domain_id,
                    item.kind_id,
                    item.key,
                    item.attribute_id,
                    item.value,
                    search.generation,
                ),
                *keys.earlier_tokens(
                    domain_id,
                    item.kind_id,
                    item.key,
                    item.attribute_id,
                    item.value,
                    search.generation,
                ),
            ]
            for item, search in pending.items()
        }
        existing = _bindings(sync, domain_id, {t for found in candidates.values() for t in found})
        retry: dict[_Plain, _Search] = {}
        for item, found in candidates.items():
            search = pending[item]
            stored = [(token, existing[token]) for token in found if token in existing]
            live = next((token for token, binding in stored if not binding.erased), None)
            if live is not None and item not in erase:
                tokens[item] = live
                continue
            if search.erased is None:
                search.erased = next((token for token, b in stored if b.erased), None)
            if search.free is None and found[0] not in existing:
                search.free = search.generation
            if not stored or search.generation + 1 >= MAX_GENERATIONS:
                mint(item, search)
                continue
            search.generation += 1
            retry[item] = search
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
                        "fingerprint_key": row.fingerprint_key,
                        "value_ciphertext": row.ciphertext,
                        "key_id": row.key_id,
                        "created_at": at,
                        "erased_at": at if row.erased else None,
                    }
                    for row in chunk
                ]
            )
            .on_conflict_do_nothing(index_elements=["domain_id", "id"])
        )


async def insert_lineage(
    session: AsyncSession, domain_id: str, lineage: Sequence[Lineage], *, at: datetime
) -> None:
    """Record key changes found by `tokenize`."""
    if not lineage:
        return
    await session.execute(
        insert(LINEAGE)
        .values(
            [
                {
                    "domain_id": domain_id,
                    "kind_id": item.kind_id,
                    "record_key": item.record_key,
                    "previous_key": item.previous_key,
                    "created_at": at,
                }
                for item in lineage
            ]
        )
        .on_conflict_do_nothing()
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
    for start in range(0, len(wanted), _CHUNK):
        rows = await session.execute(
            select(v.c.id, v.c.kind_id, v.c.attribute_id, v.c.value_ciphertext, v.c.key_id).where(
                v.c.domain_id == domain_id, v.c.id.in_(wanted[start : start + _CHUNK])
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


async def linked_keys(session: AsyncSession, domain_id: str, kind_id: str, key: str) -> list[str]:
    """`key` and every key of the Kind linked to it by `vault_lineage`, in both directions, read
    a batch of keys at a time (no limit: erasure never stops short)."""
    seen = {key}
    frontier = sorted(seen)
    lin = LINEAGE
    while frontier:
        found: set[str] = set()
        for start in range(0, len(frontier), _CHUNK):
            batch = frontier[start : start + _CHUNK]
            rows = await session.execute(
                select(lin.c.record_key, lin.c.previous_key).where(
                    lin.c.domain_id == domain_id,
                    lin.c.kind_id == kind_id,
                    (lin.c.record_key.in_(batch)) | (lin.c.previous_key.in_(batch)),
                )
            )
            found.update(k for row in rows for k in (row.record_key, row.previous_key))
        frontier = sorted(found - seen)
        seen |= found
    return sorted(seen)


@dataclass(frozen=True, slots=True)
class Erasure:
    """What an erasure did: `erased` rows erased now; `unopenable` rows it looked at that are
    sealed under a key no longer configured, whose fingerprints could not be made again (an
    erasure by value may miss a case variant among them); `keys` Record keys it covered
    (`linked`: more than `MAX_LINKED_KEYS`, which an administrator should look at: a data
    subject is rarely renamed that often)."""

    erased: int
    unopenable: int = 0
    keys: int = 1

    @property
    def linked(self) -> bool:
        return self.keys > MAX_LINKED_KEYS


async def erase_record(
    session: AsyncSession,
    domain_id: str,
    kind_id: str,
    key: str,
    attribute_ids: Collection[str] | None = None,
    *,
    keys: VaultKeys | None = None,
    at: datetime | None = None,
    lock_timeout_ms: int = 0,
) -> Erasure:
    """Erase every vaulted value a Record ever held (of `attribute_ids` only, when given), on
    every branch and Revision and under every key it had (`vault_lineage`).

    The Domain's vault lock is taken first (waiting at most `lock_timeout_ms`, else
    `VaultLockTimeoutError`): the keys are read under it, so a key change committing meanwhile
    is either seen or waits. With `keys`, the rows' fingerprints are made again under the
    current keys before they are recorded as tombstones, so those match case variants written
    later. Keys are erased a batch at a time."""
    await lock_vault_exclusive(session, domain_id, timeout_ms=lock_timeout_ms)
    v = VAULT
    found = await linked_keys(session, domain_id, kind_id, key)
    erased = unopenable = 0
    for start in range(0, len(found), _CHUNK):
        condition = [
            v.c.domain_id == domain_id,
            v.c.kind_id == kind_id,
            v.c.record_key.in_(found[start : start + _CHUNK]),
        ]
        if attribute_ids is not None:
            condition.append(v.c.attribute_id.in_(sorted(attribute_ids)))
        if keys is not None:
            unopenable += await _refresh(session, keys, condition)
        erased += await _erase(session, domain_id, condition, at)
    return Erasure(erased, unopenable, len(found))


async def erase_matching(
    session: AsyncSession,
    domain_id: str,
    kind_id: str,
    attribute_id: str,
    value: JsonValue,
    keys: VaultKeys,
    *,
    at: datetime | None = None,
    lock_timeout_ms: int = 0,
) -> Erasure:
    """Erase the canonical `value` and its case variants (same matching form) in every Record
    of the Kind. Under the Domain's vault lock (taken first, see `erase_record`), rows whose
    fingerprints were made under an earlier key or scheme are made again, so none that opens
    is missed."""
    await lock_vault_exclusive(session, domain_id, timeout_ms=lock_timeout_ms)
    v = VAULT
    scope = [v.c.domain_id == domain_id, v.c.kind_id == kind_id, v.c.attribute_id == attribute_id]
    unopenable = await _refresh(session, keys, scope)
    await session.execute(
        insert(TOMBSTONES).values(
            domain_id=domain_id,
            kind_id=kind_id,
            attribute_id=attribute_id,
            record_key=None,
            fingerprint=keys.fingerprint(domain_id, kind_id, attribute_id, value),
            fingerprint_key=keys.fingerprint_key,
            created_at=at or func.now(),
        )
    )
    fingerprints = keys.fingerprints(domain_id, kind_id, attribute_id, value)
    erased = await _erase(session, domain_id, [*scope, v.c.fingerprint.in_(fingerprints)], at)
    return Erasure(erased, unopenable)


async def mark_moved(
    session: AsyncSession, domain_id: str, moved: Sequence[tuple[str, str]]
) -> None:
    """Record the key changes `tokenize` found: token -> the key its value moved to."""
    v = VAULT
    for token, key in moved:
        await session.execute(
            update(v).where(v.c.domain_id == domain_id, v.c.id == token).values(moved_to=key)
        )


async def _refresh(session: AsyncSession, keys: VaultKeys, condition: list[Any]) -> int:
    """Make the outdated rows matching `condition` again in batches; the rows that do not
    open."""
    from lorebase_store.vault.rotation import refresh_rows

    unopenable = 0
    after: tuple[str, str] | None = None
    while True:
        report = await refresh_rows(session, keys, condition, limit=_CHUNK, after=after)
        unopenable += report.unopenable
        if report.rows < _CHUNK or report.after is None:
            return unopenable
        after = report.after


async def matching_keys(
    session: AsyncSession,
    domain_id: str,
    kind_id: str,
    attribute_id: str,
    value: JsonValue,
    keys: VaultKeys,
) -> list[str]:
    """Keys of the Records whose live vault rows hold the value or a case variant of it."""
    v = VAULT
    rows = await session.execute(
        select(v.c.record_key)
        .where(
            v.c.domain_id == domain_id,
            v.c.kind_id == kind_id,
            v.c.attribute_id == attribute_id,
            v.c.fingerprint.in_(keys.fingerprints(domain_id, kind_id, attribute_id, value)),
        )
        .distinct()
        .order_by(v.c.record_key)
    )
    return list(rows.scalars())


async def _erase(
    session: AsyncSession, domain_id: str, condition: list[Any], at: datetime | None
) -> int:
    """Erase the live rows matching `condition` and record their fingerprints."""
    v = VAULT
    erased = (
        await session.execute(
            select(
                v.c.kind_id, v.c.attribute_id, v.c.record_key, v.c.fingerprint, v.c.fingerprint_key
            )
            .where(*condition, v.c.erased_at.is_(None))
            .order_by(v.c.id)
            .with_for_update()
        )
    ).all()
    tombstones = {
        (row.kind_id, row.attribute_id, row.record_key, bytes(row.fingerprint)): row.fingerprint_key
        for row in erased
        if row.fingerprint is not None
    }
    if tombstones:
        await session.execute(
            insert(TOMBSTONES).values(
                [
                    {
                        "domain_id": domain_id,
                        "kind_id": kind_id,
                        "attribute_id": attribute_id,
                        "record_key": record_key,
                        "fingerprint": fingerprint,
                        "fingerprint_key": fingerprint_key,
                        "created_at": at or func.now(),
                    }
                    for (kind_id, attribute_id, record_key, fingerprint), fingerprint_key in sorted(
                        tombstones.items(), key=lambda item: item[0]
                    )
                ]
            )
        )
    result = await session.execute(
        update(v)
        .where(*condition, v.c.erased_at.is_(None))
        .values(
            value_ciphertext=None,
            key_id=None,
            fingerprint=None,
            fingerprint_key=None,
            erased_at=at or func.now(),
        )
        .returning(v.c.id)
    )
    return len(result.all())
