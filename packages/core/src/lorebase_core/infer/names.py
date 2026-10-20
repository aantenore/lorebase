"""Stable name normalization: table and column names to api names.

- ``normalize``: any source name to ``snake_case`` (``PurchaseOrders`` -> ``purchase_orders``,
  ``Unit Price (EUR)`` -> ``unit_price_eur``, ``2nd_line`` -> ``c_2nd_line``).
- ``singular`` / ``plural``: small English rules on the last word only.
- ``pascal``: ``purchase_order`` -> ``PurchaseOrder``; ``humanize``: ``Purchase order``.
- ``matches``: ``fnmatch`` of a normalized name against glob patterns.

The functions are pure and deterministic, so the same source names always give the same api
names.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from fnmatch import fnmatchcase

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_VOWELS = frozenset("aeiou")
#: Words whose singular and plural are the same, or that end in ``s`` in the singular.
_INVARIANT = frozenset(
    {"data", "series", "species", "news", "status", "address", "analysis", "basis", "bus"}
)
#: Fallback prefix of names that would start with a digit or be empty.
NAME_PREFIX = "c"


def normalize(name: str) -> str:
    """``snake_case`` form of a source name: ASCII letters, digits and single underscores."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    split = _CAMEL_BOUNDARY.sub("_", ascii_name.strip())
    snake = _NON_ALNUM.sub("_", split.lower()).strip("_")
    if not snake:
        return NAME_PREFIX
    if snake[0].isdigit():
        return f"{NAME_PREFIX}_{snake}"
    return snake


def strip_prefixes(name: str, prefixes: Iterable[str]) -> str:
    """``name`` without the first matching prefix, unless nothing would remain."""
    for prefix in prefixes:
        if prefix and name.startswith(prefix) and len(name) > len(prefix):
            return name[len(prefix) :]
    return name


def _last_word(snake: str) -> tuple[str, str]:
    head, sep, last = snake.rpartition("_")
    return (head + sep, last)


def singular(snake: str) -> str:
    """Singular of the last word: ``suppliers`` -> ``supplier``, ``categories`` -> ``category``."""
    head, word = _last_word(snake)
    if word in _INVARIANT or len(word) < 3:
        return snake
    if word.endswith("ies") and len(word) > 4:
        word = word[:-3] + "y"
    elif word.endswith(("sses", "shes", "ches", "xes", "zes")):
        word = word[:-2]
    elif word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    return head + word


def plural(snake: str) -> str:
    """Plural of the last word: ``order`` -> ``orders``, ``category`` -> ``categories``."""
    head, word = _last_word(snake)
    if word in _INVARIANT:
        return snake
    if word.endswith("y") and len(word) > 1 and word[-2] not in _VOWELS:
        word = word[:-1] + "ies"
    elif word.endswith(("s", "x", "z", "ch", "sh")):
        word += "es"
    else:
        word += "s"
    return head + word


def pascal(snake: str) -> str:
    """``purchase_order`` -> ``PurchaseOrder``; a leading digit gets the fallback prefix."""
    text = "".join(part[:1].upper() + part[1:] for part in snake.split("_") if part)
    if not text or not text[0].isalpha():
        return NAME_PREFIX.upper() + text
    return text


def humanize(snake: str) -> str:
    """``purchase_order`` -> ``Purchase order``."""
    words = snake.replace("_", " ").strip()
    return words[:1].upper() + words[1:]


def matches(name: str, patterns: Iterable[str]) -> bool:
    """Whether the normalized ``name`` matches one of the glob ``patterns``."""
    return any(fnmatchcase(name, pattern) for pattern in patterns)


def rank(name: str, patterns: Iterable[str]) -> int | None:
    """Index of the first pattern ``name`` matches, or ``None``."""
    for index, pattern in enumerate(patterns):
        if fnmatchcase(name, pattern):
            return index
    return None


def strip_suffix(name: str, suffixes: Iterable[str]) -> str | None:
    """``name`` without the first matching suffix (``supplier_id`` -> ``supplier``), or
    ``None`` when no suffix matches or nothing would remain."""
    for suffix in suffixes:
        if suffix and name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)].rstrip("_") or None
    return None


def unique_name(candidate: str, taken: set[str], alternatives: Iterable[str] = ()) -> str:
    """The first of ``candidate``, ``alternatives`` and ``candidate_2``, ``candidate_3``... not
    in ``taken``; the result is added to ``taken``."""
    for name in (candidate, *alternatives):
        if name not in taken:
            taken.add(name)
            return name
    index = 2
    while f"{candidate}_{index}" in taken:
        index += 1
    result = f"{candidate}_{index}"
    taken.add(result)
    return result


def unique_pascal(candidate: str, taken: set[str]) -> str:
    """PascalCase variant of ``unique_name`` (``Order``, ``Order2``)."""
    if candidate not in taken:
        taken.add(candidate)
        return candidate
    index = 2
    while f"{candidate}{index}" in taken:
        index += 1
    result = f"{candidate}{index}"
    taken.add(result)
    return result
