"""Keyed digests of inputs that may hold personal data (design 13.5).

Lorebase stores digests of some inputs it must not keep: the parameters of Operation invocations
(idempotency, provenance) and the Record keys named by erasures in the audit log. A plain hash of
such an input confirms a guessed value, so every digest is an HMAC-SHA256 under a key of the
installation, chosen in this order:

1. `vault.digest_key`;
2. the vault's current token key, when the vault is configured (the digests made before this
   module existed are unchanged);
3. the random secret revision `0027` stored in `installation_secrets` (name `digest`): it keeps
   the digests unconfirmable for whoever reads the API or the audit log, not for whoever reads
   that table. Configure one of the keys above for that.

The key of case 3 is read from the database once per process (`ready`).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.keyring import Keyring
from lorebase_store.models import metadata
from lorebase_store.vault.keys import NAMESPACE, VaultKeys, framed
from lorebase_store.vault.settings import VaultSettings

SECRET_NAME = "digest"
_SECRETS = metadata.tables["installation_secrets"]


class DigestKeysNotReadyError(RuntimeError):
    """The installation secret has not been read yet (`DigestKeys.ready`)."""


class DigestKeys:
    """Makes the keyed digests of the installation (module doc)."""

    def __init__(self, keyring: Keyring | None = None, *, source: str = "installation") -> None:
        self._keyring = keyring
        self._source = source

    @classmethod
    def from_settings(cls, settings: VaultSettings, vault: VaultKeys | None) -> DigestKeys:
        if settings.digest_key is not None:
            return cls(
                Keyring(settings.digest_key.get_secret_value(), namespace=NAMESPACE),
                source="vault.digest_key",
            )
        if vault is not None:
            return cls(vault.digest_keyring(), source="vault.token_key")
        return cls()

    @property
    def source(self) -> str:
        """Where the key comes from: `vault.digest_key`, `vault.token_key` or `installation`."""
        return self._source

    async def ready(self, session: AsyncSession) -> DigestKeys:
        """These keys, after reading the installation secret if they need it."""
        if self._keyring is None:
            secret = (
                await session.execute(
                    select(_SECRETS.c.secret).where(_SECRETS.c.name == SECRET_NAME)
                )
            ).scalar_one_or_none()
            if secret is None:
                raise DigestKeysNotReadyError(
                    "installation_secrets has no digest secret: run the migrations (revision "
                    "0027) or set vault.digest_key"
                )
            self._keyring = Keyring(secret, namespace=NAMESPACE)
        return self

    def digest(self, domain_id: str, purpose: str, payload: bytes) -> str:
        """Keyed digest (hex) of `payload` for `purpose` in a Domain."""
        if self._keyring is None:
            raise DigestKeysNotReadyError("call DigestKeys.ready first")
        return self._keyring.mac("digest/" + purpose, framed(domain_id, payload)).hex()
