"""Keys derived from one configured secret, one per namespace and purpose.

HKDF-SHA256 turns the secret into independent 256-bit keys (info `lorebase/<namespace>/<purpose>`),
so a value sealed for one purpose never opens for another. Sealing is AES-256-GCM with a random
96-bit nonce and the purpose (plus an optional context) as associated data; MACs are
HMAC-SHA256. Keeper's web sessions (`keeper.auth.sessions.encryption_key`, namespace `keeper`),
webhook signing keys at rest (`webhooks.encryption_key`, namespace `webhooks`) and the
personal data vault (`vault.encryption_key` and `vault.token_key`, namespace `vault`) use it.
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

    def seal(self, purpose: str, plaintext: bytes, *, context: bytes = b"") -> bytes:
        """Encrypt and authenticate `plaintext` for `purpose`. A non-empty `context` (such as
        the row a value is stored in) is authenticated too: the value opens only with it."""
        nonce = os.urandom(_NONCE_BYTES)
        aad = _associated(purpose, context)
        return nonce + AESGCM(self._key(purpose)).encrypt(nonce, plaintext, aad)

    def open(self, purpose: str, sealed: bytes, *, context: bytes = b"") -> bytes | None:
        """The plaintext of a value sealed for `purpose` (and `context`); None when it was
        altered, sealed for another purpose or context, or under another secret."""
        if len(sealed) <= _NONCE_BYTES:
            return None
        nonce, ciphertext = sealed[:_NONCE_BYTES], sealed[_NONCE_BYTES:]
        try:
            return AESGCM(self._key(purpose)).decrypt(
                nonce, ciphertext, _associated(purpose, context)
            )
        except InvalidTag:
            return None

    def mac(self, purpose: str, message: bytes) -> bytes:
        """HMAC-SHA256 of `message` under the key of `purpose`."""
        return hmac.new(self._key(purpose), message, hashlib.sha256).digest()


def _associated(purpose: str, context: bytes) -> bytes:
    """Associated data: the purpose alone (as before contexts existed), or purpose and context."""
    return purpose.encode() if not context else purpose.encode() + b"\x00" + context
