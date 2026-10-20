"""Three-way merge of Revisions (design 6.8 to 6.10)."""

from lorebase_core.merge.conflicts import (
    CHOICES,
    GROUPED_KINDS,
    MACHINE_RESOLVABLE,
    AutoResolution,
    Conflict,
    ConflictKind,
    Resolution,
    ResolutionChoice,
    ResolutionError,
    Side,
    UnresolvedConflictsError,
    conflict_fingerprint,
)
from lorebase_core.merge.index import TreeIndexView
from lorebase_core.merge.objects import (
    DOMAIN_KEY,
    InMemoryObjectStore,
    MissingObjectError,
    ObjectStore,
    SchemaLoader,
    load_schema,
    put_schema,
    schema_from_documents,
    tree_schema_loader,
)
from lorebase_core.merge.policies import DEFAULT_ATOMIC_PROPERTIES, MergePolicy

__all__ = [
    "CHOICES",
    "DEFAULT_ATOMIC_PROPERTIES",
    "DOMAIN_KEY",
    "GROUPED_KINDS",
    "MACHINE_RESOLVABLE",
    "AutoResolution",
    "Conflict",
    "ConflictKind",
    "InMemoryObjectStore",
    "MergePolicy",
    "MissingObjectError",
    "ObjectStore",
    "Resolution",
    "ResolutionChoice",
    "ResolutionError",
    "SchemaLoader",
    "Side",
    "TreeIndexView",
    "UnresolvedConflictsError",
    "conflict_fingerprint",
    "load_schema",
    "put_schema",
    "schema_from_documents",
    "tree_schema_loader",
]
