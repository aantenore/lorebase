"""Typed repositories: the only place where the store issues SQL (besides node and object stores).

Every repository is scoped to one Domain (and, for materialized rows, one branch) and runs in
the caller's transaction; row-level security is set with `scope_to_domain`.
"""

from lorebase_store.repositories.changesets import ChangesetRepository
from lorebase_store.repositories.domains import DomainNotFoundError, DomainRepository, DomainRow
from lorebase_store.repositories.events import (
    AuditEntry,
    AuditRepository,
    OutboxRepository,
    chain_hash,
)
from lorebase_store.repositories.index_view import (
    LinkState,
    MaterializedIndexView,
    RecordState,
)
from lorebase_store.repositories.materialized import (
    HistoryRow,
    HistoryStep,
    LinkRow,
    MaterializedRepository,
    RecordRow,
    SchemaRow,
)
from lorebase_store.repositories.provenance import ProvenanceRepository, ProvenanceRow
from lorebase_store.repositories.refs import (
    LockTimeoutError,
    RefLogRow,
    RefMovedError,
    RefNotFoundError,
    RefRepository,
    RefRow,
    advisory_lock_key,
)
from lorebase_store.repositories.revisions import (
    PgRevisionGraph,
    RevisionRepository,
    RevisionRow,
)
from lorebase_store.repositories.scope import scope_to_domain

__all__ = [
    "AuditEntry",
    "AuditRepository",
    "ChangesetRepository",
    "DomainNotFoundError",
    "DomainRepository",
    "DomainRow",
    "HistoryRow",
    "HistoryStep",
    "LinkRow",
    "LinkState",
    "LockTimeoutError",
    "MaterializedIndexView",
    "MaterializedRepository",
    "OutboxRepository",
    "PgRevisionGraph",
    "ProvenanceRepository",
    "ProvenanceRow",
    "RecordRow",
    "RecordState",
    "RefLogRow",
    "RefMovedError",
    "RefNotFoundError",
    "RefRepository",
    "RefRow",
    "RevisionRepository",
    "RevisionRow",
    "SchemaRow",
    "advisory_lock_key",
    "chain_hash",
    "scope_to_domain",
]
