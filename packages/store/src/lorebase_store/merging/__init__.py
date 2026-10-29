"""Merging Proposals over the store: merge attempts, Conflicts, resolutions, merge commits."""

from lorebase_store.merging.attempts import (
    AttemptComputation,
    ConflictState,
    Decision,
    check_resolution,
    complete_provenance,
    compute_attempt,
    decisions_of,
    taken_provenance,
)
from lorebase_store.merging.lineage import BranchLineage
from lorebase_store.merging.service import (
    MERGE_ORIGIN,
    MERGE_REASON,
    RECONCILIATION_CHECK_MODES,
    RECONCILIATION_CHECK_SETTING,
    AttemptResult,
    MergeRequest,
    MergeService,
    ProposalBranchMissingError,
    proposal_kinds,
    reconciliation_check_mode,
)

__all__ = [
    "MERGE_ORIGIN",
    "MERGE_REASON",
    "RECONCILIATION_CHECK_MODES",
    "RECONCILIATION_CHECK_SETTING",
    "AttemptComputation",
    "AttemptResult",
    "BranchLineage",
    "ConflictState",
    "Decision",
    "MergeRequest",
    "MergeService",
    "ProposalBranchMissingError",
    "check_resolution",
    "complete_provenance",
    "compute_attempt",
    "decisions_of",
    "proposal_kinds",
    "reconciliation_check_mode",
    "taken_provenance",
]
