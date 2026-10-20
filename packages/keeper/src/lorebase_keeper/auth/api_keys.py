"""API key format, hashing and verification (design 13.2).

A key looks like `<token_prefix>_<prefix>_<secret>`: `prefix` is public and finds the stored
row, `secret` is random and shown once. Only `HMAC-SHA256(pepper, secret)` is stored.

Why not a slow password hash (scrypt, argon2): the secret is 256 random bits, so guessing it is
infeasible whatever the hash costs, while a memory-hard hash on every request would let anyone
exhaust the server with bad keys. The pepper keeps the stored values useless without the server
configuration.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

from lorebase_keeper.auth.config import ApiKeySettings

_PREFIX_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
_PART = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class NewApiKey:
    """A freshly generated key. `token` is shown to the administrator once."""

    token: str
    prefix: str
    key_hash: bytes


@dataclass(frozen=True)
class ParsedKey:
    """The parts of a presented key."""

    prefix: str
    secret: str


class ApiKeyCodec:
    """Generates, parses and hashes keys according to the `api_keys` settings."""

    def __init__(self, settings: ApiKeySettings) -> None:
        pepper = settings.pepper
        if pepper is None:
            raise ValueError("api_keys.pepper is required")
        self._settings = settings
        self._pepper = pepper.get_secret_value().encode()
        self._leading = f"{settings.token_prefix}_"

    def looks_like_key(self, token: str) -> bool:
        """Whether `token` starts like one of our keys (as opposed to a JWT)."""
        return token.startswith(self._leading)

    def generate(self) -> NewApiKey:
        """A new random key."""
        prefix = "".join(
            secrets.choice(_PREFIX_ALPHABET) for _ in range(self._settings.prefix_length)
        )
        secret = base64.urlsafe_b64encode(secrets.token_bytes(self._settings.secret_bytes))
        secret_text = secret.decode().rstrip("=").replace("_", "x")
        return NewApiKey(
            token=f"{self._leading}{prefix}_{secret_text}",
            prefix=prefix,
            key_hash=self.hash_secret(secret_text),
        )

    def parse(self, token: str) -> ParsedKey | None:
        """Split a presented key; None when it is not shaped like one of ours."""
        if not self.looks_like_key(token) or not _PART.match(token):
            return None
        _, _, rest = token.partition(self._leading)
        prefix, separator, secret = rest.partition("_")
        if not separator or len(prefix) != self._settings.prefix_length or not secret:
            return None
        return ParsedKey(prefix=prefix, secret=secret)

    def hash_secret(self, secret: str) -> bytes:
        """The value stored for a secret."""
        return hmac.new(self._pepper, secret.encode(), hashlib.sha256).digest()

    def matches(self, secret: str, stored_hash: bytes) -> bool:
        """Constant-time comparison of a presented secret with a stored hash."""
        return hmac.compare_digest(self.hash_secret(secret), stored_hash)
