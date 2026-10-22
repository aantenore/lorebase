"""Materialized heads (design 6.11): incremental apply, full rebuild, branch copy or overlay,
promotion of overlays, background copies and deletions of large branches, rewrite of rows
materialized before a sensitivity tag, backfill of lineage revisions, verify."""

from lorebase_store.jobs import (
    BRANCH_DELETE_JOB,
    LINEAGE_BACKFILL_JOB,
    MATERIALIZE_JOB,
    SENSITIVITY_REFRESH_JOB,
)
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
from lorebase_store.materialize.lineage_backfill import (
    LineageBackfill,
    backfill_lineage_revisions,
    queue_lineage_sweep,
    run_lineage_backfill_job,
)
from lorebase_store.materialize.overlay import (
    OverlayError,
    promote,
    promote_overlays_of,
    refork,
)
from lorebase_store.materialize.paths import (
    refresh_path_fanout,
    refresh_stale_paths,
    run_path_fanout_sweep,
)
from lorebase_store.materialize.rebuild import RebuildReport, materialize_revision, rebuild
from lorebase_store.materialize.stale import (
    StaleRefresh,
    queue_after_tagging,
    queue_stale_refresh,
    refresh_stale_rows,
    run_sensitivity_refresh_job,
    stale_kinds,
)
from lorebase_store.materialize.verify import (
    Divergence,
    DivergenceReason,
    VerifyReport,
    verify,
)

__all__ = [
    "BRANCH_DELETE_JOB",
    "LINEAGE_BACKFILL_JOB",
    "MATERIALIZE_JOB",
    "SENSITIVITY_REFRESH_JOB",
    "BranchCreation",
    "BranchDeletingError",
    "BranchDeletion",
    "BranchExistsError",
    "BranchMode",
    "Divergence",
    "DivergenceReason",
    "LineageBackfill",
    "MaterializationMode",
    "MaterializedChange",
    "OverlayError",
    "RebuildReport",
    "StaleRefresh",
    "VerifyReport",
    "apply_changeset",
    "backfill_lineage_revisions",
    "create_branch",
    "delete_branch",
    "materialize_revision",
    "promote",
    "promote_overlays_of",
    "queue_after_tagging",
    "queue_lineage_sweep",
    "queue_materialization",
    "queue_stale_refresh",
    "rebuild",
    "refork",
    "refresh_kind",
    "refresh_path_fanout",
    "refresh_stale_paths",
    "refresh_stale_rows",
    "run_branch_delete_job",
    "run_lineage_backfill_job",
    "run_materialize_job",
    "run_path_fanout_sweep",
    "run_sensitivity_refresh_job",
    "stale_kinds",
    "verify",
]
