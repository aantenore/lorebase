"""Typed repositories: the only place where the store issues SQL (besides node and object stores).

Every repository is scoped to one Domain (and, for materialized rows, one branch) and runs in
the caller's transaction; row-level security is set with `scope_to_domain`.
"""

from lorebase_store.repositories.changesets import ChangesetRepository
from lorebase_store.repositories.documents import (
    ChunkRepository,
    ChunkRow,
    DocumentRepository,
    DocumentRow,
    ExtractionCacheRepository,
    ExtractionJobRepository,
    ExtractionJobRow,
)
from lorebase_store.repositories.domains import DomainNotFoundError, DomainRepository, DomainRow
from lorebase_store.repositories.events import OutboxRepository
from lorebase_store.repositories.index_view import (
    LinkState,
    MaterializedIndexView,
    RecordState,
)
from lorebase_store.repositories.invocations import InvocationRepository, InvocationRow
from lorebase_store.repositories.materializations import (
    BranchMaterializationRepository,
    MaterializationPhase,
    MaterializationState,
    MaterializationStatus,
    copy_fence,
)
from lorebase_store.repositories.materialized import (
    HistoryRow,
    HistoryStep,
    LinkRow,
    MaterializedRepository,
    RecordRow,
    SchemaRow,
)
from lorebase_store.repositories.merges import (
    EVERY_PARTITION,
    ConflictRepository,
    ConflictRow,
    MergeAttemptRepository,
    MergeAttemptRow,
    PartitionFilter,
    ResolutionRepository,
    ResolutionRow,
)
from lorebase_store.repositories.proposal_items import (
    ItemFilter,
    ItemRow,
    ProposalItemRepository,
)
from lorebase_store.repositories.proposals import (
    ProposalFilter,
    ProposalRepository,
    ProposalRow,
    ReviewRepository,
    ReviewRow,
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
from lorebase_store.repositories.source_runs import SourceRunRepository, SourceRunRow
from lorebase_store.repositories.suggestions import (
    RecordOutcome,
    SchemaSuggestionRepository,
    SuggestionRow,
)

__all__ = [
    "EVERY_PARTITION",
    "BranchMaterializationRepository",
    "ChangesetRepository",
    "ChunkRepository",
    "ChunkRow",
    "ConflictRepository",
    "ConflictRow",
    "DocumentRepository",
    "DocumentRow",
    "DomainNotFoundError",
    "DomainRepository",
    "DomainRow",
    "ExtractionCacheRepository",
    "ExtractionJobRepository",
    "ExtractionJobRow",
    "HistoryRow",
    "HistoryStep",
    "InvocationRepository",
    "InvocationRow",
    "ItemFilter",
    "ItemRow",
    "LinkRow",
    "LinkState",
    "LockTimeoutError",
    "MaterializationPhase",
    "MaterializationState",
    "MaterializationStatus",
    "MaterializedIndexView",
    "MaterializedRepository",
    "MergeAttemptRepository",
    "MergeAttemptRow",
    "OutboxRepository",
    "PartitionFilter",
    "PgRevisionGraph",
    "ProposalFilter",
    "ProposalItemRepository",
    "ProposalRepository",
    "ProposalRow",
    "ProvenanceRepository",
    "ProvenanceRow",
    "RecordOutcome",
    "RecordRow",
    "RecordState",
    "RefLogRow",
    "RefMovedError",
    "RefNotFoundError",
    "RefRepository",
    "RefRow",
    "ResolutionRepository",
    "ResolutionRow",
    "ReviewRepository",
    "ReviewRow",
    "RevisionRepository",
    "RevisionRow",
    "SchemaRow",
    "SchemaSuggestionRepository",
    "SourceRunRepository",
    "SourceRunRow",
    "SuggestionRow",
    "advisory_lock_key",
    "copy_fence",
    "scope_to_domain",
]
