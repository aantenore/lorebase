"""Preserved merge bases of Source Proposals (design 9.1 "Corrections that stick").

A Source run that meets a local edit (a value someone else set since the Source last wrote
it) carries, in its pending last-written state, the Attributes whose merge base must show the
value the Source last wrote. The merge of the Proposal then uses a virtual base with those
values (`MergeInput.base_overrides`), so the local edit and the upstream change are two
changes of one value: a Conflict, decided like any other. The values are read from the
Source branch Revisions the last-written state names; an Attribute the Source never wrote is
absent from the base.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import JsonValue

from lorebase_core.merge.objects import MissingObjectError, ObjectStore, load_object
from lorebase_core.revision import UnknownRevisionError
from lorebase_core.tree import NodeStore, kind_partition, tree_get
from lorebase_store.repositories.revisions import PgRevisionGraph
from lorebase_store.repositories.source_written import PreservedRow

Overrides = dict[tuple[str, str], dict[str, JsonValue]]


def base_overrides(
    rows: Sequence[PreservedRow],
    store: NodeStore,
    objects: ObjectStore,
    graph: PgRevisionGraph,
) -> Overrides:
    """`MergeInput.base_overrides` of the preserved elements of a Proposal."""
    cache: dict[tuple[bytes, str, str], Mapping[str, JsonValue] | None] = {}

    def values_at(revision: bytes, partition: str, key: str) -> Mapping[str, JsonValue] | None:
        if (revision, partition, key) in cache:
            return cache[(revision, partition, key)]
        result: Mapping[str, JsonValue] | None = None
        try:
            digest = tree_get(store, graph.root(revision), partition, key)
            if digest is not None:
                stored = load_object(objects, digest).get("values")
                result = stored if isinstance(stored, dict) else None
        except (UnknownRevisionError, MissingObjectError):
            result = None
        cache[(revision, partition, key)] = result
        return result

    overrides: Overrides = {}
    for row in rows:
        pending, current = row.pending, row.current
        partition = kind_partition(pending.element)
        values: dict[str, JsonValue] = {}
        for attribute in pending.preserve:
            revision = None
            if current is not None and attribute in current.written:
                revision = current.revision_of(attribute)
            found = None if revision is None else values_at(revision, partition, pending.key)
            values[attribute] = None if found is None else found.get(attribute)
        overrides[(partition, pending.key)] = values
    return overrides
