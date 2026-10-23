"""Kind-scoped invisibility (design 13.3): the schema a caller limited to some Kinds reads.

A caller whose grants are narrowed to Kinds reads the projection of the schema to those Kinds
(`lorebase_core.expr.visibility.visible_schema`): names are resolved against it, so a Kind
the caller may not read, and everything that would tell about it, fails like a name that
does not exist (`keeper.auth.authz.invisible_kinds`).

Projections are remembered per schema object and Kinds (compiled schemas are cached, so the
same object comes back for the same schema), like the effective sensitivity tags.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock

from lorebase_core.expr.visibility import element_ids, visible_schema
from lorebase_core.model import Schema


@dataclass(frozen=True, slots=True)
class Projection:
    """A schema as a reader of some Kinds sees it, and the ids it leaves out."""

    schema: Schema
    hidden: frozenset[str]


_Key = tuple[int, frozenset[str]]
_VISIBLE: OrderedDict[_Key, tuple[Schema, Projection]] = OrderedDict()
_VISIBLE_ENTRIES = 256
_VISIBLE_LOCK = Lock()


def projection(schema: Schema, kinds: frozenset[str]) -> Projection:
    """`schema` as a reader of the Kinds `kinds` (api names) sees it."""
    key = (id(schema), kinds)
    with _VISIBLE_LOCK:
        found = _VISIBLE.get(key)
        if found is not None and found[0] is schema:
            _VISIBLE.move_to_end(key)
            return found[1]
    seen = visible_schema(schema, kinds)
    hidden = frozenset() if seen is schema else frozenset(element_ids(schema) - element_ids(seen))
    projected = Projection(seen, hidden)
    with _VISIBLE_LOCK:
        _VISIBLE[key] = (schema, projected)
        while len(_VISIBLE) > _VISIBLE_ENTRIES:
            _VISIBLE.popitem(last=False)
    return projected


def visible(schema: Schema, kinds: frozenset[str]) -> Schema:
    """`schema` as a reader of the Kinds `kinds` sees it (`projection`)."""
    return projection(schema, kinds).schema
