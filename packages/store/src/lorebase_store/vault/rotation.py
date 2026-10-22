"""Vault key rotation (design 13.5): rows sealed or fingerprinted under earlier keys are made
again under the current ones.

A row is outdated when its value is sealed under another key than `vault.key_id`, or its
fingerprint was made under another token key or scheme than the current ones
(`VaultKeys.fingerprint_key`). `rotate` walks the live rows of every Domain in `(domain_id, id)`
order, `vault.rekey_batch` rows per transaction, opens each outdated one, seals it again under
the current key with the same token (Record objects do not change) and makes its fingerprint
again. The position it stopped at is returned, so a job continues where the last one stopped;
rows locked by a concurrent erasure are skipped and found by the next pass. A row whose key is
no longer configured cannot be opened: it is counted as `unopenable` and left as it is.

`usage` counts live rows by encryption key and fingerprint key and tombstones by fingerprint
key: an encryption key can be removed from `previous_keys` once no live row uses it, a token key
from `previous_token_keys` once no live row and no tombstone uses it (tombstones cannot be made
again: they hold no value; dropping a token key retires the tombstones made under it).
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_store.auth.scope import rls_scope
from lorebase_store.vault.keys import VaultKeys
from lorebase_store.vault.store import TOMBSTONES, VAULT


@dataclass(slots=True)
class RotationReport:
    """What a rotation pass did; `after` is where the next pass starts (None: done)."""

    rows: int = 0
    resealed: int = 0
    fingerprinted: int = 0
    unopenable: int = 0
    after: tuple[str, str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "resealed": self.resealed,
            "fingerprinted": self.fingerprinted,
            "unopenable": self.unopenable,
            "after": None if self.after is None else list(self.after),
            "done": self.after is None,
        }


@dataclass(frozen=True, slots=True)
class KeyUsage:
    """Live rows by encryption key id and by fingerprint key, tombstones by fingerprint key
    (`<token_key_id>/<scheme>`, `legacy` for rows written before rotation)."""

    encryption_keys: dict[str, int] = field(default_factory=dict)
    fingerprint_keys: dict[str, int] = field(default_factory=dict)
    tombstone_keys: dict[str, int] = field(default_factory=dict)

    def outdated(self, keys: VaultKeys) -> int:
        """Live rows the rotation still has to make again (upper bound: a row can be both)."""
        sealed = sum(n for k, n in self.encryption_keys.items() if k != keys.key_id)
        fingerprinted = sum(
            n for k, n in self.fingerprint_keys.items() if k != keys.fingerprint_key
        )
        return max(sealed, fingerprinted)


LEGACY = "legacy"


def _outdated(keys: VaultKeys) -> Any:
    v = VAULT
    return or_(
        v.c.key_id != keys.key_id,
        v.c.fingerprint_key.is_(None),
        v.c.fingerprint_key != keys.fingerprint_key,
    )


async def refresh_rows(
    session: AsyncSession,
    keys: VaultKeys,
    condition: Sequence[Any],
    *,
    limit: int | None = None,
    after: tuple[str, str] | None = None,
    skip_locked: bool = False,
) -> RotationReport:
    """Make the outdated live rows matching `condition` again (module doc), in the caller's
    transaction; at most `limit` rows after `after`."""
    v = VAULT
    query = (
        select(
            v.c.domain_id,
            v.c.id,
            v.c.kind_id,
            v.c.attribute_id,
            v.c.key_id,
            v.c.value_ciphertext,
            v.c.fingerprint_key,
        )
        .where(*condition, v.c.value_ciphertext.is_not(None), _outdated(keys))
        .order_by(v.c.domain_id, v.c.id)
        .with_for_update(skip_locked=skip_locked)
    )
    if after is not None:
        query = query.where(tuple_(v.c.domain_id, v.c.id) > tuple_(*after))
    if limit is not None:
        query = query.limit(limit)
    report = RotationReport()
    for row in (await session.execute(query)).all():
        report.rows += 1
        report.after = (row.domain_id, row.id)
        opened = keys.open(row.domain_id, row.id, row.key_id, bytes(row.value_ciphertext))
        if opened is None:
            report.unopenable += 1
            continue
        value = json.loads(opened)
        changes: dict[str, Any] = {}
        if row.key_id != keys.key_id:
            changes["value_ciphertext"] = keys.seal(row.domain_id, row.id, value)
            changes["key_id"] = keys.key_id
            report.resealed += 1
        if row.fingerprint_key != keys.fingerprint_key:
            changes["fingerprint"] = keys.fingerprint(
                row.domain_id, row.kind_id, row.attribute_id, value
            )
            changes["fingerprint_key"] = keys.fingerprint_key
            report.fingerprinted += 1
        await session.execute(
            update(v).where(v.c.domain_id == row.domain_id, v.c.id == row.id).values(**changes)
        )
    return report


async def rotate(
    sessions: async_sessionmaker[AsyncSession],
    keys: VaultKeys,
    *,
    batch: int,
    max_rows: int,
    after: tuple[str, str] | None = None,
) -> RotationReport:
    """Make outdated rows of every Domain again, `batch` per transaction, until `max_rows` rows
    were looked at or none is left (module doc)."""
    total = RotationReport(after=after)
    position = after
    while total.rows < max_rows:
        async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
            report = await refresh_rows(
                session,
                keys,
                [],
                limit=min(batch, max_rows - total.rows),
                after=position,
                skip_locked=True,
            )
        total.rows += report.rows
        total.resealed += report.resealed
        total.fingerprinted += report.fingerprinted
        total.unopenable += report.unopenable
        if report.after is None:
            total.after = None
            return total
        position = total.after = report.after
    return total


async def usage(session: AsyncSession) -> KeyUsage:
    """Key usage of every Domain (module doc); runs with the row-level security bypass."""
    v, t = VAULT, TOMBSTONES
    async with rls_scope(session, bypass=True):
        sealed = (
            await session.execute(
                select(v.c.key_id, func.count())
                .where(v.c.value_ciphertext.is_not(None))
                .group_by(v.c.key_id)
            )
        ).all()
        fingerprinted = (
            await session.execute(
                select(v.c.fingerprint_key, func.count())
                .where(v.c.value_ciphertext.is_not(None))
                .group_by(v.c.fingerprint_key)
            )
        ).all()
        tombstones = (
            await session.execute(
                select(t.c.fingerprint_key, func.count()).group_by(t.c.fingerprint_key)
            )
        ).all()
    return KeyUsage(
        encryption_keys=_counts(sealed),
        fingerprint_keys=_counts(fingerprinted),
        tombstone_keys=_counts(tombstones),
    )


def _counts(rows: Sequence[Any]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for key, count in rows:
        counts[key or LEGACY] += int(count)
    return dict(sorted(counts.items()))
