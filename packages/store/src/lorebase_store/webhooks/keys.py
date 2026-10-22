"""Webhook signing keys at rest.

The signing key of a webhook is `sha256(secret)` (`webhooks.signing`): whoever holds it can
sign deliveries. With `webhooks.encryption_key` set, it is stored sealed (AES-256-GCM under a
key derived from that secret, `lorebase_store.keyring`) in `webhooks.sealed_key`, and
`secret_hash` stays null. Without it, the key is stored usable in `secret_hash`, as before
revision 0016; the delivery worker seals such rows once a key is configured
(`seal_stored_keys`), so existing webhooks keep working through the change. Keys sealed under
one of `previous_encryption_keys` still open and are sealed again under the current key.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.keyring import Keyring
from lorebase_store.models import metadata
from lorebase_store.webhooks.settings import WebhookSettings

WEBHOOKS = metadata.tables["webhooks"]
NAMESPACE = "webhooks"
PURPOSE = "signing-key"


@dataclass(frozen=True, slots=True)
class StoredKey:
    """The columns that hold a signing key: exactly one is set."""

    secret_hash: bytes | None
    sealed_key: bytes | None


@dataclass(frozen=True, slots=True)
class OpenedKey:
    """A usable signing key; `stale` when it is stored usable or under a previous key."""

    key: bytes
    stale: bool


class SigningKeys:
    """Seals and opens webhook signing keys under the configured encryption keys."""

    def __init__(self, current: str | None, previous: Sequence[str] = ()) -> None:
        self._current = None if current is None else Keyring(current, namespace=NAMESPACE)
        self._previous = [Keyring(secret, namespace=NAMESPACE) for secret in previous]

    @classmethod
    def from_settings(cls, settings: WebhookSettings) -> SigningKeys:
        current = settings.encryption_key
        return cls(
            None if current is None else current.get_secret_value(),
            [key.get_secret_value() for key in settings.previous_encryption_keys],
        )

    @property
    def sealing(self) -> bool:
        """Whether new keys are stored sealed."""
        return self._current is not None

    @property
    def rotating(self) -> bool:
        """Whether keys sealed under a previous encryption key may exist."""
        return bool(self._previous)

    def store(self, key: bytes) -> StoredKey:
        """The columns to store `key` in."""
        if self._current is None:
            return StoredKey(secret_hash=key, sealed_key=None)
        return StoredKey(secret_hash=None, sealed_key=self._current.seal(PURPOSE, key))

    def open(self, stored: StoredKey) -> OpenedKey | None:
        """The usable key; None when it is sealed under a key that is not configured."""
        if stored.secret_hash is not None:
            return OpenedKey(bytes(stored.secret_hash), stale=self._current is not None)
        if stored.sealed_key is None:
            return None
        sealed = bytes(stored.sealed_key)
        if self._current is not None:
            key = self._current.open(PURPOSE, sealed)
            if key is not None:
                return OpenedKey(key, stale=False)
        for keyring in self._previous:
            key = keyring.open(PURPOSE, sealed)
            if key is not None:
                return OpenedKey(key, stale=True)
        return None


async def seal_stored_keys(session: AsyncSession, keys: SigningKeys, *, limit: int) -> int:
    """Seal up to `limit` keys stored usable or under a previous key; the number sealed.

    Runs in the caller's transaction, which must see every Domain (RLS bypass). Rows are locked
    with `SKIP LOCKED`, so concurrent workers seal different rows.
    """
    if not keys.sealing:
        return 0
    w = WEBHOOKS
    query = (
        select(w.c.domain_id, w.c.id, w.c.secret_hash, w.c.sealed_key)
        .order_by(w.c.domain_id, w.c.id)
        .with_for_update(skip_locked=True)
    )
    if not keys.rotating:  # only keys stored usable are stale: found without opening any
        query = query.where(w.c.secret_hash.is_not(None)).limit(limit)
    rows = list(await session.execute(query))
    sealed = 0
    for row in rows:
        if sealed >= limit:
            break
        opened = keys.open(StoredKey(row.secret_hash, row.sealed_key))
        if opened is None or not opened.stale:
            continue
        stored = keys.store(opened.key)
        await session.execute(
            update(w)
            .where(w.c.domain_id == row.domain_id, w.c.id == row.id)
            .values(secret_hash=stored.secret_hash, sealed_key=stored.sealed_key)
        )
        sealed += 1
    return sealed
