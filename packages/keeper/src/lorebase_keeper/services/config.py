"""Settings of the Domain, branch, schema and Operation endpoints (`keeper.domains`, ...).

They are sub-sections of the `keeper` section; see `config/lorebase.example.yaml`.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from lorebase_core.infer import InferenceOptions
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
        description="Expressions are not enabled yet: when true, CEL preconditions are skipped "
        "(reported as warnings) instead of refusing the execution.",
    )

    @model_validator(mode="after")
    def _batch_within_bulk(self) -> Self:
        if self.import_batch_rows > self.max_bulk_rows:
            raise ValueError("import_batch_rows cannot exceed max_bulk_rows")
        return self


class ProposalSettings(SectionModel):
    """Proposals, reviews and merges (design 6.10, 10.3).

    A Domain overrides `required_approvals` with its `required_approvals` setting; its
    `merge_policy` setting holds the merge policy options of new Proposals.
    """

    required_approvals: int = Field(
        default=1,
        ge=0,
        description="Approvals a Proposal needs before it merges into a protected branch.",
    )
    allow_self_approval: bool = Field(
        default=False,
        description="Whether authors may approve their own Proposals into protected branches; "
        "when false their approvals are refused and never counted.",
    )
    changes_requested_blocks: bool = Field(
        default=True,
        description="Whether an outstanding `request_changes` review blocks merges into "
        "protected branches.",
    )
    auto_close_source_branch: bool = Field(
        default=False,
        description="Delete the source branch once its Proposal merged (never the default "
        "branch or a protected one).",
    )
    attempt_retention: int = Field(
        default=5,
        ge=1,
        description="Merge attempts kept per Proposal; older superseded ones are deleted "
        "(merged attempts are always kept).",
    )
    preview_on_create: bool = Field(
        default=True,
        description="Compute the merge preview (Conflict count) when a Proposal is created.",
    )
    max_resolutions: int = Field(
        default=1000, ge=1, description="Resolutions accepted by one bulk request."
    )


class SchemaInferenceSettings(SectionModel):
    """Schema inference from uploaded CSV files or Postgres tables (design 9.1, tier 0).

    Uploads are also bounded by `keeper.limits.max_request_body_bytes`.
    """

    max_files: int = Field(default=50, ge=1, description="Files accepted by one request.")
    max_file_bytes: int = Field(
        default=1_048_576, ge=1, description="Largest file accepted, in bytes (413 above)."
    )
    max_total_bytes: int = Field(
        default=1_048_576, ge=1, description="Largest sum of file sizes, in bytes (413 above)."
    )
    file_suffixes: tuple[str, ...] = Field(
        default=(".csv", ".tsv", ".txt"), min_length=1, description="Accepted file name suffixes."
    )
    allowed_sources: tuple[str, ...] = Field(
        default=(),
        description="Secret references (secret://env/NAME, secret://file/PATH) holding Postgres "
        "URLs that callers may name as sources. Empty: Postgres inference is disabled.",
    )
    max_issues: int = Field(default=500, ge=1, description="Issues listed in one response.")
    options: InferenceOptions = Field(
        default_factory=InferenceOptions,
        description="Thresholds and naming rules; a request may override some of them.",
    )
