"""Keys derived from `keeper.auth.sessions.encryption_key`, one per purpose.

HKDF-SHA256 turns the configured secret into independent 256-bit keys, so a value sealed for
one purpose never opens for another. Sealing is AES-256-GCM with a random 96-bit nonce and the
purpose as associated data; MACs are HMAC-SHA256. `cryptography` is already required by
PyJWT's crypto extra.
"""

from __future__ import annotations

import hashlib
import hmac
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_NONCE_BYTES = 12
_INFO_PREFIX = b"lorebase/keeper/"


class Keyring:
    """Seals, opens and authenticates values under keys derived from one secret."""

    def __init__(self, secret: str) -> None:
        self._secret = secret.encode()
        self._keys: dict[str, bytes] = {}

    def _key(self, purpose: str) -> bytes:
        key = self._keys.get(purpose)
        if key is None:
            key = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=None,
                info=_INFO_PREFIX + purpose.encode(),
            ).derive(self._secret)
            self._keys[purpose] = key
        return key

    def seal(self, purpose: str, plaintext: bytes) -> bytes:
        """Encrypt and authenticate `plaintext` for `purpose`."""
        nonce = os.urandom(_NONCE_BYTES)
        return nonce + AESGCM(self._key(purpose)).encrypt(nonce, plaintext, purpose.encode())

    def open(self, purpose: str, sealed: bytes) -> bytes | None:
        """The plaintext of a value sealed for `purpose`; None when it was altered, sealed for
        another purpose or under another secret."""
        if len(sealed) <= _NONCE_BYTES:
            return None
        nonce, ciphertext = sealed[:_NONCE_BYTES], sealed[_NONCE_BYTES:]
        try:
            return AESGCM(self._key(purpose)).decrypt(nonce, ciphertext, purpose.encode())
        except InvalidTag:
            return None

    def mac(self, purpose: str, message: bytes) -> bytes:
        """HMAC-SHA256 of `message` under the key of `purpose`."""
        return hmac.new(self._key(purpose), message, hashlib.sha256).digest()
