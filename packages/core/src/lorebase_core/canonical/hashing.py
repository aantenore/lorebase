"""Lorebase canonical form and content hashing (design 6.1, AC-09)."""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Mapping, Sequence
from enum import StrEnum

from lorebase_core.canonical.jcs import CanonicalError, jcs_bytes

__all__ = [
    "HASH_SCHEME",
    "HASH_SIZE",
    "NODE_HASH_TYPE",
    "REVISION_HASH_TYPE",
    "ObjectType",
    "canonical_bytes",
    "from_hex",
    "hex",
    "node_hash",
    "normalize",
    "object_hash",
    "revision_hash",
]

HASH_SCHEME = "lb1"
"""Version tag of the hashing scheme. Changing any hashing rule requires a new tag."""

HASH_SIZE = hashlib.sha256().digest_size
"""Size in bytes of every Lorebase hash."""

NODE_HASH_TYPE = "node"
REVISION_HASH_TYPE = "revision"

_SEPARATOR = b"\x00"


class ObjectType(StrEnum):
    """Types of content-addressed objects (design 6.1)."""

    SCHEMA_KIND = "schema.kind"
    SCHEMA_RELATION = "schema.relation"
    SCHEMA_TRAIT = "schema.trait"
    SCHEMA_OPERATION = "schema.operation"
    SCHEMA_DERIVATION = "schema.derivation"
    SCHEMA_SOURCE = "schema.source"
    SCHEMA_DOMAIN = "schema.domain"
    RECORD = "record"
    LINK = "link"


_HASHED_TYPES = frozenset({*(t.value for t in ObjectType), REVISION_HASH_TYPE})


def normalize(value: object) -> object:
    """Apply the Lorebase value rules before RFC 8785 serialization.

    Strings (including object keys) are NFC-normalized and object members whose
    value is ``None`` are omitted, since null and absent are the same. ``None``
    inside arrays is kept, because array positions are significant.
    Integers and decimals of Attribute values are expected to arrive already
    encoded as canonical strings; timestamps are taken as given.
    """
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalError(f"object keys must be strings, got {type(key).__name__}")
            if item is None:
                continue
            nfc_key = unicodedata.normalize("NFC", key)
            if nfc_key in result:
                raise CanonicalError(f"duplicate object key after NFC normalization: {nfc_key!r}")
            result[nfc_key] = normalize(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [normalize(item) for item in value]
    return value


def canonical_bytes(value: object) -> bytes:
    """Return the Lorebase canonical JSON bytes of ``value`` (RFC 8785 plus Lorebase rules)."""
    return jcs_bytes(normalize(value))


def object_hash(object_type: str, value: object) -> bytes:
    """Return ``sha256("lb1\\x00" + object_type + "\\x00" + canonical_bytes(value))``.

    ``object_type`` is an :class:`ObjectType` or ``"revision"``.
    """
    if object_type not in _HASHED_TYPES:
        raise ValueError(f"unknown object type: {object_type!r}")
    return _scoped_hash(str(object_type), canonical_bytes(value))


def revision_hash(value: object) -> bytes:
    """Hash a canonical Revision payload (design 6.4)."""
    return _scoped_hash(REVISION_HASH_TYPE, canonical_bytes(value))


def node_hash(body: bytes) -> bytes:
    """Hash an encoded tree node: ``sha256("lb1\\x00node\\x00" + body)`` (design 6.2)."""
    return _scoped_hash(NODE_HASH_TYPE, body)


def hex(h: bytes) -> str:
    """Render a hash as lowercase hex, the API representation."""
    return h.hex()


def from_hex(text: str) -> bytes:
    """Parse a lowercase hex hash produced by :func:`hex`."""
    if len(text) != 2 * HASH_SIZE or text != text.lower():
        raise ValueError(f"expected {2 * HASH_SIZE} lowercase hex characters")
    return bytes.fromhex(text)


def _scoped_hash(scope: str, body: bytes) -> bytes:
    digest = hashlib.sha256()
    digest.update(HASH_SCHEME.encode("ascii"))
    digest.update(_SEPARATOR)
    digest.update(scope.encode("ascii"))
    digest.update(_SEPARATOR)
    digest.update(body)
    return digest.digest()
