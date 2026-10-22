"""Tokens, fingerprints and encryption of vault values (design 13.5).

- **Token** of a value: `vlt_` + the first 128 bits of HMAC-SHA256 (key derived from
  `vault.token_key`) over the Domain, Kind, Record key, Attribute, generation and the value's
  canonical JSON. The same value of the same Attribute of the same Record gets the same token,
  so writing it again changes nothing and merges compare tokens as they would compare values.
  Two Records holding the same value get different tokens: erasing one data subject never
  erases another's value. The generation starts at 0 and moves on only when the token of a
  lower one was erased, so writing an erased value again stores it under a new token and the
  Revisions that held the erased one keep reading `erased`.
- **Fingerprint** of a value: HMAC-SHA256 over the Domain, Kind, Attribute and the value's
  matching form (`lorebase_core.types.matching`: case, compatibility characters and white space
  ignored), without the Record key; erasure by value finds the rows of every Record holding it,
  whichever way it was written. Rows written before matching forms existed hold the fingerprint
  of the canonical value (scheme 1); the rotation job moves them to the current scheme.
- **Encryption**: AES-256-GCM under a key derived from `vault.encryption_key` (or one of
  `previous_keys`, by the row's `key_id`), with the Domain and token as associated data, so a
  ciphertext copied to another row does not open.

Both keys rotate. A new `encryption_key` seals new values; `lorebase_store.vault.rotation`
seals the stored ones again. A new `token_key` (with its own `token_key_id`, the old one kept in
`previous_token_keys`) mints new tokens: a value written again first looks for the token it got
under each earlier key, so unchanged values stay unchanged, and fingerprints are matched under
every configured token key until the rotation job has made them again under the current one.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import JsonValue

from lorebase_core.canonical import jcs_bytes
from lorebase_core.model.vault import TOKEN_PREFIX
from lorebase_core.types import matching_form
from lorebase_store.keyring import Keyring
from lorebase_store.vault.settings import VaultSettings

NAMESPACE = "vault"
_TOKEN = "token"
_FINGERPRINT = "fingerprint"
_MATCHING = "fingerprint/2"
FINGERPRINT_SCHEME = 2
"""Current fingerprint scheme: HMAC of the matching form. Scheme 1 hashed the canonical value."""
_VALUE = "value"
_TOKEN_BYTES = 16


def framed(*parts: str | bytes) -> bytes:
    """Length-prefixed concatenation: no two part lists give the same bytes."""
    out = bytearray()
    for part in parts:
        data = part.encode() if isinstance(part, str) else part
        out += len(data).to_bytes(4, "big") + data
    return bytes(out)


class VaultKeys:
    """Derives tokens and fingerprints and seals values under the configured keys."""

    def __init__(
        self,
        token_key: str,
        key_id: str,
        current: str,
        previous: Mapping[str, str],
        *,
        token_key_id: str = "1",
        previous_token_keys: Mapping[str, str] | None = None,
    ):
        self._token_key_id = token_key_id
        self._token_keys = {token_key_id: Keyring(token_key, namespace=NAMESPACE)}
        for previous_id, secret in (previous_token_keys or {}).items():
            self._token_keys[previous_id] = Keyring(secret, namespace=NAMESPACE)
        self._tokens = self._token_keys[token_key_id]
        self._key_id = key_id
        self._keyrings = {key_id: Keyring(current, namespace=NAMESPACE)}
        for previous_id, secret in previous.items():
            self._keyrings[previous_id] = Keyring(secret, namespace=NAMESPACE)

    @classmethod
    def from_settings(cls, settings: VaultSettings) -> VaultKeys | None:
        """Keys of the settings; None when the vault is not configured."""
        if settings.token_key is None or settings.encryption_key is None:
            return None
        return cls(
            settings.token_key.get_secret_value(),
            settings.key_id,
            settings.encryption_key.get_secret_value(),
            {k: v.get_secret_value() for k, v in settings.previous_keys.items()},
            token_key_id=settings.token_key_id,
            previous_token_keys={
                k: v.get_secret_value() for k, v in settings.previous_token_keys.items()
            },
        )

    @property
    def key_id(self) -> str:
        """Id of the key new values are sealed under."""
        return self._key_id

    @property
    def key_ids(self) -> frozenset[str]:
        """Ids of every configured encryption key."""
        return frozenset(self._keyrings)

    @property
    def token_key_id(self) -> str:
        """Id of the key new tokens and fingerprints are made under."""
        return self._token_key_id

    @property
    def token_key_ids(self) -> frozenset[str]:
        """Ids of every configured token key."""
        return frozenset(self._token_keys)

    @property
    def fingerprint_key(self) -> str:
        """What a row stores of how its fingerprint was made: `<token_key_id>/<scheme>`."""
        return f"{self._token_key_id}/{FINGERPRINT_SCHEME}"

    def token(
        self,
        domain_id: str,
        kind_id: str,
        key: str,
        attribute_id: str,
        value: JsonValue,
        generation: int = 0,
    ) -> str:
        """The token of a value under the current token key."""
        return _token(self._tokens, domain_id, kind_id, key, attribute_id, value, generation)

    def earlier_tokens(
        self,
        domain_id: str,
        kind_id: str,
        key: str,
        attribute_id: str,
        value: JsonValue,
        generation: int = 0,
    ) -> list[str]:
        """The tokens the value had under the previous token keys."""
        return [
            _token(keyring, domain_id, kind_id, key, attribute_id, value, generation)
            for key_id, keyring in self._token_keys.items()
            if key_id != self._token_key_id
        ]

    def fingerprint(
        self, domain_id: str, kind_id: str, attribute_id: str, value: JsonValue
    ) -> bytes:
        """Fingerprint of a canonical value under the current token key and scheme."""
        return _matching(self._tokens, domain_id, kind_id, attribute_id, value)

    def fingerprints(
        self, domain_id: str, kind_id: str, attribute_id: str, value: JsonValue
    ) -> list[bytes]:
        """Every fingerprint a row holding the value (or a case variant of it, for rows of the
        current scheme) may carry: each configured token key, current and earlier scheme."""
        found: list[bytes] = []
        for keyring in self._token_keys.values():
            found.append(_matching(keyring, domain_id, kind_id, attribute_id, value))
            found.append(
                keyring.mac(
                    _FINGERPRINT, framed(domain_id, kind_id, attribute_id, jcs_bytes(value))
                )
            )
        return found

    def digest_keyring(self) -> Keyring:
        """The keyring of the keyed digests when no `vault.digest_key` is set."""
        return self._tokens

    def earlier_digest_keyrings(self) -> tuple[Keyring, ...]:
        """The keyrings of the previous token keys, which made earlier digests."""
        return tuple(
            keyring for key_id, keyring in self._token_keys.items() if key_id != self._token_key_id
        )

    def seal(self, domain_id: str, token: str, value: JsonValue) -> bytes:
        return self._keyrings[self._key_id].seal(
            _VALUE, jcs_bytes(value), context=framed(domain_id, token)
        )

    def open(self, domain_id: str, token: str, key_id: str, sealed: bytes) -> bytes | None:
        """Canonical JSON of the value; None when the key id is not configured or the
        ciphertext does not open."""
        keyring = self._keyrings.get(key_id)
        if keyring is None:
            return None
        return keyring.open(_VALUE, sealed, context=framed(domain_id, token))


def _token(
    keyring: Keyring,
    domain_id: str,
    kind_id: str,
    key: str,
    attribute_id: str,
    value: JsonValue,
    generation: int,
) -> str:
    message = framed(domain_id, kind_id, key, attribute_id, str(generation), jcs_bytes(value))
    return TOKEN_PREFIX + keyring.mac(_TOKEN, message)[:_TOKEN_BYTES].hex()


def _matching(
    keyring: Keyring,
    domain_id: str,
    kind_id: str,
    attribute_id: str,
    value: JsonValue,
) -> bytes:
    form = jcs_bytes(matching_form(value))
    return keyring.mac(_MATCHING, framed(domain_id, kind_id, attribute_id, form))
