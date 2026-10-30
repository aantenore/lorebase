"""The compared form of words, values and names (design 8.9).

Resolution, the value index and vocabulary suggestions compare text in one form: lower case
(final sigma folded), Latin combining marks dropped after NFD, blanks collapsed. The value
index computes the same form in SQL (`lb_value_norm`, revision 0045); `normalize` is its Python
twin, so both sides of a comparison agree.
"""

from __future__ import annotations

import re
import unicodedata

_COMBINING = re.compile(f"[{chr(0x300)}-{chr(0x36F)}]")
BLANKS = "".join(
    chr(c)
    for c in (
        *range(0x09, 0x0E), *range(0x1C, 0x21), 0x85, 0xA0, 0x1680, *range(0x2000, 0x200B),
        0x2028, 0x2029, 0x202F, 0x205F, 0x3000,
    )
)  # fmt: skip
"""The blanks `lb_value_norm` collapses: what `str.split()` takes for white space."""
_BLANKS = re.compile(f"[{re.escape(BLANKS)}]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def normalize(value: str) -> str:
    """The compared form of a value or a word, as `lb_value_norm` computes it: lower case
    (final sigma folded), Latin combining marks dropped after NFD, blanks collapsed."""
    lowered = value.lower().replace(chr(0x3C2), chr(0x3C3))  # final sigma
    decomposed = unicodedata.normalize("NFD", lowered)
    return _BLANKS.sub(" ", _COMBINING.sub("", decomposed)).strip(" ")


def name_words(api_name: str) -> str:
    """An api name as words: `unit_price` and `UnitPrice` give `unit price`."""
    return normalize(_CAMEL.sub(" ", api_name).replace("_", " ").replace(".", " "))


__all__ = ["BLANKS", "name_words", "normalize"]
