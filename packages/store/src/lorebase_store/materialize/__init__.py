"""Materialized heads (design 6.11): incremental apply, full rebuild, branch copy or overlay,
promotion of overlays, verify."""

from lorebase_store.jobs import MATERIALIZE_JOB
from lorebase_store.materialize.apply import MaterializedChange, apply_changeset, refresh_kind
from lorebase_store.materialize.branch import (
    BranchCreation,
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
    run_materialize_job,
)
from lorebase_store.materialize.rebuild import RebuildReport, materialize_revision, rebuild
from lorebase_store.materialize.verify import (
    Divergence,
    DivergenceReason,
    VerifyReport,
    verify,
)

__all__ = [
    "MATERIALIZE_JOB",
    "BranchCreation",
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
    "materialize_revision",
    "promote",
    "promote_overlays_of",
    "rebuild",
    "refork",
    "refresh_kind",
    "run_materialize_job",
    "verify",
]
