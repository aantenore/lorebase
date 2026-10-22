"""Matching form of canonical values (design 13.5): the form two values are compared in when
erasure looks for "the same value" in other Records.

The canonical encoding of each Attribute type already makes equal values equal: numbers,
decimals, dates, timestamps and durations have one spelling, text is NFC. People write the same
personal value in more ways than that, so the matching form also folds every string in the
value (top level, array elements, members of `json` values): compatibility characters (NFKC),
letter case and runs of white space are ignored. Two values with the same matching form are
erased together. The form does not depend on the Attribute's type, so a fingerprint made at
write time and one made again later (key rotation) always agree.

The matching form is only ever hashed (fingerprints): it is never stored or shown.
"""

from __future__ import annotations

import re
import unicodedata

from lorebase_core.types.types import CanonicalValue

_SPACES = re.compile(r"\s+")


def fold_text(text: str) -> str:
    """Text without the differences matching ignores: compatibility forms, case and runs of
    white space (trimmed at both ends)."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return _SPACES.sub(" ", folded).strip()


def matching_form(value: CanonicalValue) -> CanonicalValue:
    """The matching form of a canonical value (module doc)."""
    if isinstance(value, str):
        return fold_text(value)
    if isinstance(value, list):
        return [matching_form(item) for item in value]
    if isinstance(value, dict):
        return {key: matching_form(item) for key, item in value.items()}
    return value
