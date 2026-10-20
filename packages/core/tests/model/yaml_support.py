"""Helpers shared by the YAML schema tests."""

from __future__ import annotations

import copy
import itertools
from collections.abc import Callable
from typing import Any

from lorebase_core.model import IdPrefix, new_id

SECTIONS = ("kinds", "relations", "traits", "operations", "derivations", "sources")


def counting_factory() -> Callable[[IdPrefix], str]:
    """An id factory that mints a predictable sequence of ids."""
    counter = itertools.count(1)

    def mint(prefix: IdPrefix) -> str:
        return new_id(prefix, timestamp_ms=1, randomness=next(counter).to_bytes(10, "big"))

    return mint


def strip_ids(document: dict[str, Any]) -> dict[str, Any]:
    """The document as a person would first write it: no element ids."""
    stripped = copy.deepcopy(document)
    for section in SECTIONS:
        for element in stripped.get(section, {}).values():
            element.pop("id")
            for attribute in element.get("attributes", {}).values():
                attribute.pop("id")
    return stripped
