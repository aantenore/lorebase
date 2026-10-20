"""Changesets: the delta of a Revision against its first parent (design 6.3)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from lorebase_core.canonical import HASH_SIZE
from lorebase_core.revision.revision import RevisionStats
from lorebase_core.tree import NodeStore, TreeChange, tree_diff

__all__ = ["ChangesetEntry", "changeset_between", "changeset_stats", "changeset_tree_changes"]

_NO_PROVENANCE: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class ChangesetEntry:
    """One changed element: ``None`` hashes mean absent.

    ``provenance`` maps each changed Attribute id to its provenance entry id;
    it is not part of any content hash (AC-08).
    """

    partition: str
    key: str
    old_hash: bytes | None
    new_hash: bytes | None
    provenance: Mapping[str, str] = field(default=_NO_PROVENANCE, compare=False)

    def __post_init__(self) -> None:
        if self.old_hash == self.new_hash:
            raise ValueError("a changeset entry must change the element")
        for h in (self.old_hash, self.new_hash):
            if h is not None and len(h) != HASH_SIZE:
                raise ValueError(f"hashes are {HASH_SIZE} bytes")


def changeset_between(
    store: NodeStore, parent_root: bytes | None, root: bytes
) -> list[ChangesetEntry]:
    """Changeset of ``root`` against its first parent's root, without provenance."""
    return [
        ChangesetEntry(d.partition, d.key, d.hash_a, d.hash_b)
        for d in tree_diff(store, parent_root, root)
    ]


def changeset_tree_changes(entries: Iterable[ChangesetEntry]) -> list[TreeChange]:
    """Tree changes that turn the parent tree into the Revision tree."""
    return [TreeChange(e.partition, e.key, e.new_hash) for e in entries]


def changeset_stats(entries: Iterable[ChangesetEntry]) -> RevisionStats:
    """Revision stats derived from a changeset."""
    partitions: set[str] = set()
    changes = 0
    for entry in entries:
        partitions.add(entry.partition)
        changes += 1
    return RevisionStats(partitions_changed=len(partitions), changes=changes)
