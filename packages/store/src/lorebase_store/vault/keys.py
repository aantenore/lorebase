"""Tokens, fingerprints and encryption of vault values (design 13.5).

- **Token** of a value: `vlt_` + the first 128 bits of HMAC-SHA256 (key derived from
  `vault.token_key`) over the Domain, Kind, Record key, Attribute, generation and the value's
  canonical JSON. The same value of the same Attribute of the same Record gets the same token,
  so writing it again changes nothing and merges compare tokens as they would compare values.
  Two Records holding the same value get different tokens: erasing one data subject never
  erases another's value. The generation starts at 0 and moves on only when the token of a
  lower one was erased, so writing an erased value again stores it under a new token and the
  Revisions that held the erased one keep reading `erased`.
- **Fingerprint** of a value: HMAC-SHA256 over the Domain, Kind, Attribute and canonical value,
  without the Record key; erasure by value finds the rows of every Record holding it.
- **Encryption**: AES-256-GCM under a key derived from `vault.encryption_key` (or one of
  `previous_keys`, by the row's `key_id`), with the Domain and token as associated data, so a
  ciphertext copied to another row does not open.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import JsonValue

from lorebase_core.canonical import jcs_bytes
from lorebase_core.model.vault import TOKEN_PREFIX
from lorebase_store.keyring import Keyring
from lorebase_store.vault.settings import VaultSettings

NAMESPACE = "vault"
_TOKEN = "token"
_FINGERPRINT = "fingerprint"
_VALUE = "value"
_TOKEN_BYTES = 16


def _message(*parts: str | bytes) -> bytes:
    """Length-prefixed concatenation: no two part lists give the same bytes."""
    out = bytearray()
    for part in parts:
        data = part.encode() if isinstance(part, str) else part
        out += len(data).to_bytes(4, "big") + data
    return bytes(out)


class VaultKeys:
    """Derives tokens and fingerprints and seals values under the configured keys."""

    def __init__(self, token_key: str, key_id: str, current: str, previous: Mapping[str, str]):
        self._tokens = Keyring(token_key, namespace=NAMESPACE)
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
        )

    @property
    def key_id(self) -> str:
        """Id of the key new values are sealed under."""
        return self._key_id

    def token(
        self,
        domain_id: str,
        kind_id: str,
        key: str,
        attribute_id: str,
        value: JsonValue,
        generation: int = 0,
    ) -> str:
        message = _message(domain_id, kind_id, key, attribute_id, str(generation), jcs_bytes(value))
        return TOKEN_PREFIX + self._tokens.mac(_TOKEN, message)[:_TOKEN_BYTES].hex()

    def fingerprint(
        self, domain_id: str, kind_id: str, attribute_id: str, value: JsonValue
    ) -> bytes:
        return self._tokens.mac(
            _FINGERPRINT, _message(domain_id, kind_id, attribute_id, jcs_bytes(value))
        )

    def digest(self, domain_id: str, purpose: str, payload: bytes) -> str:
        """Keyed digest (hex) of `payload` for `purpose` in a Domain: a stored digest of
        inputs that may hold personal data cannot be reversed by guessing them."""
        return self._tokens.mac("digest/" + purpose, _message(domain_id, payload)).hex()

    def seal(self, domain_id: str, token: str, value: JsonValue) -> bytes:
        return self._keyrings[self._key_id].seal(
            _VALUE, jcs_bytes(value), context=_message(domain_id, token)
        )

    def open(self, domain_id: str, token: str, key_id: str, sealed: bytes) -> bytes | None:
        """Canonical JSON of the value; None when the key id is not configured or the
        ciphertext does not open."""
        keyring = self._keyrings.get(key_id)
        if keyring is None:
            return None
        return keyring.open(_VALUE, sealed, context=_message(domain_id, token))
