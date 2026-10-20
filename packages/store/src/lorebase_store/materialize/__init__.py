"""Materialized heads (design 6.11): incremental apply, full rebuild, branch copy, verify."""

from lorebase_store.materialize.apply import MaterializedChange, apply_changeset, refresh_kind
from lorebase_store.materialize.branch import (
    BranchCreation,
    BranchExistsError,
    MaterializationMode,
    create_branch,
)
from lorebase_store.materialize.rebuild import RebuildReport, materialize_revision, rebuild
from lorebase_store.materialize.verify import (
    Divergence,
    DivergenceReason,
    VerifyReport,
    verify,
)

__all__ = [
    "BranchCreation",
    "BranchExistsError",
    "Divergence",
    "DivergenceReason",
    "MaterializationMode",
    "MaterializedChange",
    "RebuildReport",
    "VerifyReport",
    "apply_changeset",
    "create_branch",
    "materialize_revision",
    "rebuild",
    "refresh_kind",
    "verify",
]
