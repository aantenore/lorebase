"""Closed value sets stored as native Postgres enum types."""

from __future__ import annotations

from enum import StrEnum

from lorebase_store.models.base import pg_enum


class RefKind(StrEnum):
    """`branch` moves; `tag` is an immutable pointer (design 6.5); `snapshot` holds the
    materialized rows of a past Revision for reads at it (`materialize.snapshot`)."""

    BRANCH = "branch"
    TAG = "tag"
    SNAPSHOT = "snapshot"


class Materialization(StrEnum):
    """Branch materialization mode (design 6.11)."""

    FULL = "full"
    OVERLAY = "overlay"
    NONE = "none"


class ProposalOrigin(StrEnum):
    """What opened a Proposal."""

    MANUAL = "manual"
    SOURCE_SYNC = "source_sync"
    EXTRACTION = "extraction"
    OPERATION = "operation"


class ProposalStatus(StrEnum):
    """Proposal lifecycle."""

    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"


class ReviewVerdict(StrEnum):
    """Outcome of one Proposal review."""

    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    COMMENT = "comment"


class ItemDecision(StrEnum):
    """Decision on one extraction Proposal item."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class MergeAttemptStatus(StrEnum):
    """Merge attempt lifecycle (design 6.8 step 5, 6.10)."""

    CONFLICTED = "conflicted"
    STALE = "stale"
    RESOLVED = "resolved"
    MERGED = "merged"
    ABANDONED = "abandoned"


class PrincipalType(StrEnum):
    """Kind of authenticated actor (design 13.2)."""

    USER = "user"
    SERVICE = "service"
    API_KEY = "api_key"


class GrantScope(StrEnum):
    """What a grant applies to (design 10.2)."""

    DOMAIN = "domain"
    KIND = "kind"
    OPERATION = "operation"
    BRANCH = "branch"


class InvocationStatus(StrEnum):
    """Operation invocation status (design 10.1)."""

    PENDING = "pending"
    COMMITTED = "committed"
    PROPOSED = "proposed"
    REJECTED = "rejected"
    DRY_RUN = "dry_run"
    FAILED = "failed"


class JobStatus(StrEnum):
    """Background job lifecycle; `queued` jobs are claimed with SKIP LOCKED."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunStatus(StrEnum):
    """Lifecycle of Source runs and extraction jobs."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


REF_KIND = pg_enum(RefKind, "ref_kind")
MATERIALIZATION = pg_enum(Materialization, "materialization")
PROPOSAL_ORIGIN = pg_enum(ProposalOrigin, "proposal_origin")
PROPOSAL_STATUS = pg_enum(ProposalStatus, "proposal_status")
REVIEW_VERDICT = pg_enum(ReviewVerdict, "review_verdict")
ITEM_DECISION = pg_enum(ItemDecision, "item_decision")
MERGE_ATTEMPT_STATUS = pg_enum(MergeAttemptStatus, "merge_attempt_status")
PRINCIPAL_TYPE = pg_enum(PrincipalType, "principal_type")
GRANT_SCOPE = pg_enum(GrantScope, "grant_scope")
INVOCATION_STATUS = pg_enum(InvocationStatus, "invocation_status")
JOB_STATUS = pg_enum(JobStatus, "job_status")
RUN_STATUS = pg_enum(RunStatus, "run_status")
