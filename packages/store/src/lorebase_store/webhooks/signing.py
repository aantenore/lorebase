"""Webhook secrets and signatures.

A webhook secret is shown once, when the webhook is created. Lorebase keeps only its SHA-256
digest, which is also the HMAC key: receivers compute `sha256(secret)` once and verify

    signature header = "t=<unix seconds>,v1=<hex HMAC-SHA256(key, "<t>." + body)>"

The timestamp lets receivers refuse replays older than their tolerance. Storing the digest
keeps the secret itself out of the database; a leaked digest still allows forging signatures,
so the digest is stored sealed when `webhooks.encryption_key` is set (`webhooks.keys`). Rotate
a webhook (delete and create) when the database and that key may have been exposed.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

SECRET_PREFIX = "whsec_"
SIGNATURE_VERSION = "v1"


def new_secret(nbytes: int) -> str:
    """A new random webhook secret."""
    return SECRET_PREFIX + secrets.token_urlsafe(nbytes)


def signing_key(secret: str) -> bytes:
    """The stored digest of a secret, used as the HMAC key."""
    return hashlib.sha256(secret.encode()).digest()


def signature(key: bytes, timestamp: int, body: bytes) -> str:
    """Value of the signature header for `body` sent at `timestamp`."""
    digest = hmac.new(key, f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},{SIGNATURE_VERSION}={digest}"


def verify(secret: str, header: str, body: bytes, *, now: int, tolerance_s: int) -> bool:
    """Receiver side: whether `header` signs `body` with `secret` within the tolerance."""
    parts = dict(part.split("=", 1) for part in header.split(",") if "=" in part)
    try:
        timestamp = int(parts["t"])
    except (KeyError, ValueError):
        return False
    if abs(now - timestamp) > tolerance_s:
        return False
    expected = signature(signing_key(secret), timestamp, body)
    return hmac.compare_digest(expected, header)
