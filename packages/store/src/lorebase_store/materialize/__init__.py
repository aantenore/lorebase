"""Materialized heads (design 6.11): incremental apply, full rebuild, branch copy or overlay,
promotion of overlays, background copies and deletions of large branches, verify."""

from lorebase_store.jobs import BRANCH_DELETE_JOB, MATERIALIZE_JOB
from lorebase_store.materialize.apply import MaterializedChange, apply_changeset, refresh_kind
from lorebase_store.materialize.background import (
    BranchDeletion,
    delete_branch,
    queue_materialization,
    run_branch_delete_job,
    run_materialize_job,
)
from lorebase_store.materialize.branch import (
    BranchCreation,
    BranchDeletingError,
    BranchExistsError,
    BranchMode,
    MaterializationMode,
    create_branch,
)
from lorebase_store.materialize.overlay import (
    OverlayError,
    promote,
    promote_overlays_of,
    refork,
)
from lorebase_store.materialize.rebuild import RebuildReport, materialize_revision, rebuild
from lorebase_store.materialize.verify import (
    Divergence,
    DivergenceReason,
    VerifyReport,
    verify,
)

__all__ = [
    "BRANCH_DELETE_JOB",
    "MATERIALIZE_JOB",
    "BranchCreation",
    "BranchDeletingError",
    "BranchDeletion",
    "BranchExistsError",
    "BranchMode",
    "Divergence",
    "DivergenceReason",
    "MaterializationMode",
    "MaterializedChange",
    "OverlayError",
    "RebuildReport",
    "VerifyReport",
    "apply_changeset",
    "create_branch",
    "delete_branch",
    "materialize_revision",
    "promote",
    "promote_overlays_of",
    "queue_materialization",
    "rebuild",
    "refork",
    "refresh_kind",
    "run_branch_delete_job",
    "run_materialize_job",
    "verify",
]
