"""Settings of the Domain, branch, schema and Operation endpoints (`keeper.domains`, ...).

They are sub-sections of the `keeper` section; see `config/lorebase.example.yaml`.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from lorebase_core.expr import ExprLimits
from lorebase_core.tree import DEFAULT_LEAF_SIZE
from lorebase_platform.settings import SectionModel

DEFAULT_REF_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$"


class DomainSettings(SectionModel):
    """Domain creation."""

    leaf_size: int = Field(
        default=DEFAULT_LEAF_SIZE,
        ge=2,
        description="Tree leaf size of new Domains; fixed for the life of a Domain.",
    )
    creator_role: str | None = Field(
        default="admin",
        description="Role granted in a new Domain to the principal that created it; null "
        "grants nothing (installation administrators then grant roles).",
    )
    protect_default_branch: bool = Field(
        default=True, description="Whether the default branch of a new Domain is protected."
    )


class BranchSettings(SectionModel):
    """Branch and tag management."""

    materialization: Literal["full"] = Field(
        default="full",
        description="Materialization of new branches. M1 supports `full` only: rows are copied "
        "from the source branch when the branch starts at its head, rebuilt from the tree "
        "otherwise.",
    )
    max_materialized_per_domain: int = Field(
        default=20, ge=1, description="Fully materialized branches allowed per Domain."
    )
    name_pattern: str = Field(
        default=DEFAULT_REF_NAME_PATTERN,
        min_length=1,
        description="Regular expression branch and tag names must match.",
    )
    max_name_length: int = Field(default=200, ge=1)
    lock_retry_after_s: int = Field(
        default=1,
        ge=0,
        description="Retry-After sent when a branch stays locked by another writer (503).",
    )


class SchemaChangeSettings(SectionModel):
    """Schema change sets."""

    max_issues: int = Field(default=200, ge=1, description="Issues listed in one response.")
    max_listed_changes: int = Field(
        default=1000, ge=1, description="Element changes listed in one response."
    )


class ReadSettings(SectionModel):
    """Authorization of reads that name a Revision or tag."""

    reachability_max_depth: int = Field(
        default=1000,
        ge=1,
        description="Parent hops followed from a branch head when checking that a Revision "
        "or tag named by a caller with branch-scoped grants is reachable from a branch the "
        "caller may read. A Revision further back than this is refused (403).",
    )


class OperationSettings(SectionModel):
    """Operation execution (design 10.1)."""

    max_bulk_rows: int = Field(
        default=10000, ge=1, description="Rows accepted by one import_records or import_links."
    )
    import_batch_rows: int = Field(
        default=1000, ge=1, description="Rows committed per Revision by bulk imports."
    )
    dry_run_max_edits: int = Field(
        default=1000, ge=1, description="Edits listed in a response; longer lists are cut."
    )
    max_issues: int = Field(default=200, ge=1, description="Violations listed in one response.")
    direct_commit_on_protected: bool = Field(
        default=False,
        description="Whether Operations whose review policy is `never` commit directly to "
        "protected branches. When false they are refused and must go through a Proposal.",
    )
    skip_expression_preconditions: bool = Field(
        default=False,
        description="When expressions are not enabled: true skips CEL preconditions (reported "
        "as warnings) instead of refusing the execution.",
    )
    expressions_enabled: bool = Field(
        default=False,
        description="Evaluate the CEL preconditions, expression review policies and steps of "
        "stored Operations with the expression engine. When false, Operations that need CEL "
        "are refused (expressions-not-enabled).",
    )
    expression_limits: ExprLimits = Field(
        default_factory=ExprLimits,
        description="Budget of one expression: source length, nesting depth, evaluation "
        "steps and time, sizes of built strings and lists, Record lookups, decimal precision.",
    )

    @model_validator(mode="after")
    def _batch_within_bulk(self) -> Self:
        if self.import_batch_rows > self.max_bulk_rows:
            raise ValueError("import_batch_rows cannot exceed max_bulk_rows")
        return self
