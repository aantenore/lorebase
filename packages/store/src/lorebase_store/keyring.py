"""Keys derived from one configured secret, one per namespace and purpose.

HKDF-SHA256 turns the secret into independent 256-bit keys (info `lorebase/<namespace>/<purpose>`),
so a value sealed for one purpose never opens for another. Sealing is AES-256-GCM with a random
96-bit nonce and the purpose as associated data; MACs are HMAC-SHA256. Keeper's web sessions
(`keeper.auth.sessions.encryption_key`, namespace `keeper`) and webhook signing keys at rest
(`webhooks.encryption_key`, namespace `webhooks`) use it.
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


class Keyring:
    """Seals, opens and authenticates values under keys derived from one secret."""

    def __init__(self, secret: str, *, namespace: str) -> None:
        self._secret = secret.encode()
        self._info = f"lorebase/{namespace}/".encode()
        self._keys: dict[str, bytes] = {}

    def _key(self, purpose: str) -> bytes:
        key = self._keys.get(purpose)
        if key is None:
            key = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=None,
                info=self._info + purpose.encode(),
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
