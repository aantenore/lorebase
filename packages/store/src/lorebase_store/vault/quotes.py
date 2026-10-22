"""Which texts hold a vaulted value, without opening any (extraction evidence, design 9.2).

Word sequences of a text are encoded as the Attribute's type canonicalizes values and
fingerprinted like stored values (`VaultKeys.fingerprints`: the matching form, under every
configured token key and fingerprint scheme, so case, compatibility characters and white space
do not matter); a text matches when a live vault row of that Kind and Attribute has one of the
fingerprints, whichever Record holds it. Nothing is decrypted. A value written differently from
its canonical form in the text (a date spelled out, a reordered name) is not found.

The check fails closed: a text that exceeds its limits counts as a match.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Schema
from lorebase_core.types import AttributeType, EncodingError, encode_value
from lorebase_store.vault.keys import VaultKeys
from lorebase_store.vault.store import VAULT, vault_attributes

_UNQUOTED_TYPES = frozenset(
    {AttributeType.JSON, AttributeType.ARRAY, AttributeType.GEO_POINT, AttributeType.BOOLEAN}
)
_WORD = re.compile(r"\S+")
_EDGE = " \t\r\n.,;:!?()[]{}<>\"'`*_|"
_POSSESSIVE = re.compile(r"['\N{RIGHT SINGLE QUOTATION MARK}]s$", re.IGNORECASE)
_LOOKUP_BATCH = 500


@dataclass(frozen=True, slots=True)
class QuoteLimits:
    """Bounds of one `quoted_values` call; a text that exceeds one is reported as a match."""

    max_words: int
    """Longest word sequence of a text compared with the stored values."""
    max_text_words: int
    """A text with more words is not checked."""
    max_text_chars: int
    """A text with more characters is not checked."""
    max_candidates: int
    """Distinct word sequences of all the texts together."""


def _candidates(text: str, max_words: int) -> set[str]:
    """Word sequences of `text` (at most `max_words` words, single-spaced), as written, with
    the punctuation at their edges stripped, and without a closing possessive."""
    words = _WORD.findall(text)
    found: set[str] = set()
    for start in range(len(words)):
        for end in range(start + 1, min(len(words), start + max_words) + 1):
            candidate = " ".join(words[start:end])
            found.add(candidate)
            stripped = candidate.strip(_EDGE)
            if stripped:
                found.add(stripped)
                bare = _POSSESSIVE.sub("", stripped)
                if bare:
                    found.add(bare)
    return found


async def has_vault_values(session: AsyncSession, domain_id: str) -> bool:
    """Whether the Domain has a live vault row."""
    v = VAULT
    found = await session.scalar(
        select(v.c.id).where(v.c.domain_id == domain_id, v.c.erased_at.is_(None)).limit(1)
    )
    return found is not None


async def quoted_values(
    session: AsyncSession,
    keys: VaultKeys,
    domain_id: str,
    schema: Schema,
    texts: Sequence[str],
    limits: QuoteLimits,
) -> set[int]:
    """Indexes of the `texts` to withhold: those that hold a live vault value of any vault
    Attribute of the schema (the whole Domain), and those that could not be checked within
    `limits`."""
    vaulted = vault_attributes(schema)
    hits: set[int] = set()
    owners: dict[str, set[int]] = defaultdict(set)
    for index, text in enumerate(texts):
        if len(text) > limits.max_text_chars or len(text.split()) > limits.max_text_words:
            hits.add(index)
            continue
        found = _candidates(text, limits.max_words)
        if len(owners) + len(found - owners.keys()) > limits.max_candidates:
            hits.update(range(index, len(texts)))
            break
        for candidate in found:
            owners[candidate].add(index)
    if not vaulted or not owners:
        return hits
    v = VAULT
    for kind_id, attribute_ids in vaulted.items():
        for attribute_id in attribute_ids:
            spec = schema.kinds[kind_id].attributes[attribute_id].type
            if spec.name in _UNQUOTED_TYPES:
                continue
            by_fingerprint: dict[bytes, set[int]] = defaultdict(set)
            for candidate, indexes in owners.items():
                try:
                    canonical = encode_value(spec, candidate)
                except EncodingError:
                    continue
                for fingerprint in keys.fingerprints(domain_id, kind_id, attribute_id, canonical):
                    by_fingerprint[fingerprint] |= indexes
            wanted = list(by_fingerprint)
            for offset in range(0, len(wanted), _LOOKUP_BATCH):
                rows = await session.scalars(
                    select(v.c.fingerprint)
                    .where(
                        v.c.domain_id == domain_id,
                        v.c.kind_id == kind_id,
                        v.c.attribute_id == attribute_id,
                        v.c.erased_at.is_(None),
                        v.c.fingerprint.in_(wanted[offset : offset + _LOOKUP_BATCH]),
                    )
                    .distinct()
                )
                for fingerprint in rows:
                    hits |= by_fingerprint.get(bytes(fingerprint), set())
    return hits
