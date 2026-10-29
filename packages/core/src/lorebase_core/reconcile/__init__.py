"""Reconciliation of Source contradictions (design 10.6): pure functions shared by the
detection triggers in Scribe and the decisions in Keeper.

Claims and source identities (``claims``), candidate types, statuses and fingerprints, period
classification and claim folding (``candidates``), the normalization stage (``normalize``) and
the options of the pipeline (``options``). Nothing here decides a candidate or changes a value.
"""

from lorebase_core.reconcile.candidates import (
    CANDIDATE_ID_PREFIX,
    TRANSITIONS,
    CandidateDraft,
    CandidateStatus,
    CandidateType,
    DecisionAction,
    MergedClaims,
    Signal,
    candidate_fingerprint,
    classify,
    disagree,
    merge_claims,
    new_candidate_id,
)
from lorebase_core.reconcile.claims import (
    Claim,
    IdentityKind,
    ProposalItemRef,
    identity,
    source_identity,
    value_hash,
)
from lorebase_core.reconcile.normalize import (
    Comparison,
    Normalized,
    Quantity,
    UnitTable,
    UnitTableError,
    compare,
    fold_words,
    normalize_claims,
    numbers_equal,
    parse_quantity,
)
from lorebase_core.reconcile.options import (
    STAGE_ORDER,
    DuplicateOptions,
    NormalizeOptions,
    ReconcileOptions,
    SemanticOptions,
    Stage,
    StaleOptions,
    TriggerOptions,
)

__all__ = [
    "CANDIDATE_ID_PREFIX",
    "STAGE_ORDER",
    "TRANSITIONS",
    "CandidateDraft",
    "CandidateStatus",
    "CandidateType",
    "Claim",
    "Comparison",
    "DecisionAction",
    "DuplicateOptions",
    "IdentityKind",
    "MergedClaims",
    "NormalizeOptions",
    "Normalized",
    "ProposalItemRef",
    "Quantity",
    "ReconcileOptions",
    "SemanticOptions",
    "Signal",
    "Stage",
    "StaleOptions",
    "TriggerOptions",
    "UnitTable",
    "UnitTableError",
    "candidate_fingerprint",
    "classify",
    "compare",
    "disagree",
    "fold_words",
    "identity",
    "merge_claims",
    "new_candidate_id",
    "normalize_claims",
    "numbers_equal",
    "parse_quantity",
    "source_identity",
    "value_hash",
]
