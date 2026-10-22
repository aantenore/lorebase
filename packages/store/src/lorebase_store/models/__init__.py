"""SQLAlchemy models of every Lorebase table (design 7). `metadata` is the migration target."""

from __future__ import annotations

from lorebase_store.models.base import Base
from lorebase_store.models.governance import (
    ApiKey,
    ApiKeyUsage,
    AuditEntry,
    Grant,
    Group,
    GroupMember,
    OperationInvocation,
    Principal,
    WebSession,
)
from lorebase_store.models.ingestion import (
    Document,
    DocumentBlob,
    DocumentChunk,
    ExtractionCacheEntry,
    ExtractionJob,
    SourceRun,
    VaultEntry,
)
from lorebase_store.models.materialized import Embedding, MatHistory, MatLink, MatRecord, MatSchema
from lorebase_store.models.proposals import (
    Conflict,
    ConflictResolution,
    MergeAttempt,
    Proposal,
    ProposalItem,
    ProposalReview,
)
from lorebase_store.models.queues import Job, OutboxEvent
from lorebase_store.models.versioning import (
    ChangesetEntry,
    Domain,
    Provenance,
    Ref,
    RefLogEntry,
    Revision,
    RevisionParent,
    StoredObject,
    TreeNode,
)
from lorebase_store.models.webhooks import Webhook, WebhookDelivery

metadata = Base.metadata

__all__ = [
    "ApiKey",
    "ApiKeyUsage",
    "AuditEntry",
    "Base",
    "ChangesetEntry",
    "Conflict",
    "ConflictResolution",
    "Document",
    "DocumentBlob",
    "DocumentChunk",
    "Domain",
    "Embedding",
    "ExtractionCacheEntry",
    "ExtractionJob",
    "Grant",
    "Group",
    "GroupMember",
    "Job",
    "MatHistory",
    "MatLink",
    "MatRecord",
    "MatSchema",
    "MergeAttempt",
    "OperationInvocation",
    "OutboxEvent",
    "Principal",
    "Proposal",
    "ProposalItem",
    "ProposalReview",
    "Provenance",
    "Ref",
    "RefLogEntry",
    "Revision",
    "RevisionParent",
    "SourceRun",
    "StoredObject",
    "TreeNode",
    "VaultEntry",
    "WebSession",
    "Webhook",
    "WebhookDelivery",
    "metadata",
]
