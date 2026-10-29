"""Stable ids (design 5.1, AC-01): prefixed ULIDs such as ``knd_01JB8Z...``."""

from __future__ import annotations

import secrets
import time
from enum import StrEnum
from typing import Annotated

from pydantic import StringConstraints

CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ULID_LENGTH = 26
ULID_REGEX = "[0-7][0-9A-HJKMNP-TV-Z]{25}"
_TIMESTAMP_BITS = 48
_RANDOM_BYTES = 10


class IdPrefix(StrEnum):
    KIND = "knd"
    ATTRIBUTE = "att"
    RELATION = "rel"
    TRAIT = "trt"
    OPERATION = "opr"
    DERIVATION = "drv"
    SOURCE = "src"
    PROVENANCE = "prv"
    LENS = "lns"


KindId = Annotated[str, StringConstraints(pattern=rf"^knd_{ULID_REGEX}$")]
AttributeId = Annotated[str, StringConstraints(pattern=rf"^att_{ULID_REGEX}$")]
RelationId = Annotated[str, StringConstraints(pattern=rf"^rel_{ULID_REGEX}$")]
TraitId = Annotated[str, StringConstraints(pattern=rf"^trt_{ULID_REGEX}$")]
OperationId = Annotated[str, StringConstraints(pattern=rf"^opr_{ULID_REGEX}$")]
DerivationId = Annotated[str, StringConstraints(pattern=rf"^drv_{ULID_REGEX}$")]
SourceId = Annotated[str, StringConstraints(pattern=rf"^src_{ULID_REGEX}$")]
LensId = Annotated[str, StringConstraints(pattern=rf"^lns_{ULID_REGEX}$")]
ProvenanceId = Annotated[str, StringConstraints(pattern=rf"^prv_{ULID_REGEX}$")]


def new_ulid(*, timestamp_ms: int | None = None, randomness: bytes | None = None) -> str:
    """Mint a ULID. Pass ``timestamp_ms`` and ``randomness`` (10 bytes) for deterministic ids."""
    millis = time.time_ns() // 1_000_000 if timestamp_ms is None else timestamp_ms
    if not 0 <= millis < 2**_TIMESTAMP_BITS:
        raise ValueError("ULID timestamp must fit in 48 bits")
    entropy = secrets.token_bytes(_RANDOM_BYTES) if randomness is None else randomness
    if len(entropy) != _RANDOM_BYTES:
        raise ValueError(f"ULID randomness must be {_RANDOM_BYTES} bytes")
    number = (millis << (8 * _RANDOM_BYTES)) | int.from_bytes(entropy, "big")
    chars = []
    for _ in range(ULID_LENGTH):
        number, digit = divmod(number, 32)
        chars.append(CROCKFORD_ALPHABET[digit])
    return "".join(reversed(chars))


def new_id(
    prefix: IdPrefix, *, timestamp_ms: int | None = None, randomness: bytes | None = None
) -> str:
    """Mint a prefixed id, for example ``new_id(IdPrefix.KIND)`` -> ``knd_01JB8Z...``."""
    return f"{prefix.value}_{new_ulid(timestamp_ms=timestamp_ms, randomness=randomness)}"


def is_ulid(text: str) -> bool:
    return (
        len(text) == ULID_LENGTH
        and text[0] in CROCKFORD_ALPHABET[:8]
        and all(ch in CROCKFORD_ALPHABET for ch in text)
    )


def id_prefix(element_id: str) -> IdPrefix | None:
    """The prefix of a well-formed id, or ``None``."""
    prefix, _, rest = element_id.partition("_")
    try:
        parsed = IdPrefix(prefix)
    except ValueError:
        return None
    return parsed if is_ulid(rest) else None
