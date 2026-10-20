"""Revisions, changesets, ancestry and merge base (design 6.3, 6.4, 6.8)."""

from lorebase_core.revision.ancestry import (
    InMemoryRevisionGraph,
    MergeBaseError,
    MergeBaseResult,
    MergeStatus,
    RevisionGraph,
    UnknownRevisionError,
    VirtualBaseBuilder,
    VirtualRevision,
    is_ancestor,
    lowest_common_ancestors,
    merge_base,
)
from lorebase_core.revision.changeset import (
    ChangesetEntry,
    changeset_between,
    changeset_stats,
    changeset_tree_changes,
)
from lorebase_core.revision.revision import (
    ROOT_GENERATION,
    Author,
    Hash,
    Origin,
    Revision,
    RevisionStats,
    format_timestamp,
    next_generation,
    revision_id,
)

__all__ = [
    "ROOT_GENERATION",
    "Author",
    "ChangesetEntry",
    "Hash",
    "InMemoryRevisionGraph",
    "MergeBaseError",
    "MergeBaseResult",
    "MergeStatus",
    "Origin",
    "Revision",
    "RevisionGraph",
    "RevisionStats",
    "UnknownRevisionError",
    "VirtualBaseBuilder",
    "VirtualRevision",
    "changeset_between",
    "changeset_stats",
    "changeset_tree_changes",
    "format_timestamp",
    "is_ancestor",
    "lowest_common_ancestors",
    "merge_base",
    "next_generation",
    "revision_id",
]
