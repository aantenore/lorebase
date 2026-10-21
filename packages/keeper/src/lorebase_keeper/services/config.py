"""Settings of the Domain, branch, schema and Operation endpoints (`keeper.domains`, ...).

They are sub-sections of the `keeper` section; see `config/lorebase.example.yaml`.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from lorebase_core.expr import ExprLimits
from lorebase_core.infer import InferenceOptions
from lorebase_core.model import SECRET_REFERENCE_PREFIX, DomainSlug
from lorebase_core.tree import DEFAULT_LEAF_SIZE
from lorebase_platform.settings import SectionModel
from lorebase_store.models.enums import PrincipalType

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
        description="When expressions are not enabled: true skips CEL preconditions (reported "
        "as warnings) instead of refusing the execution.",
    )
    expressions_enabled: bool = Field(
        default=True,
        description="Evaluate the CEL preconditions, expression review policies and steps of "
        "stored Operations with the expression engine (sandboxed: no I/O, deterministic, "
        "bounded by expression_limits). When false, Operations that need CEL are refused "
        "(expressions-not-enabled). Whether schemas may hold CEL at all is "
        "`schema.expressions_enabled`.",
    )
    expression_limits: ExprLimits = Field(
        default_factory=ExprLimits,
        description="Budget of one expression evaluated by an Operation: source length, "
        "nesting depth, evaluation steps and time, sizes of built strings and lists, Record "
        "and Link lookups, decimal precision.",
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
    max_item_decisions: int = Field(
        default=5000,
        ge=1,
        description="Extraction items one `items:decide` request may decide (listed or "
        "selected by filter).",
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
        "URLs that callers may name as sources. The default of every Domain not listed in "
        "`domain_sources`. Empty: Postgres inference is disabled for those Domains.",
    )
    domain_sources: dict[DomainSlug, tuple[str, ...]] = Field(
        default_factory=dict,
        description="Per-Domain source lists keyed by Domain id. A listed Domain may name only "
        "its own secret references (an empty list disables Postgres inference for it); "
        "`allowed_sources` does not apply to it.",
    )
    max_issues: int = Field(default=500, ge=1, description="Issues listed in one response.")
    options: InferenceOptions = Field(
        default_factory=InferenceOptions,
        description="Thresholds and naming rules; a request may override some of them.",
    )

    @field_validator("domain_sources")
    @classmethod
    def _secret_references(cls, value: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        for domain_id, sources in value.items():
            for source in sources:
                if not source.startswith(SECRET_REFERENCE_PREFIX):
                    raise ValueError(
                        f"sources of Domain {domain_id!r} must be secret references "
                        f"such as {SECRET_REFERENCE_PREFIX}env/NAME"
                    )
        return value

    def sources_for(self, domain_id: str) -> tuple[str, ...]:
        """The secret references a Domain may name: its own list, or the default."""
        return self.domain_sources.get(domain_id, self.allowed_sources)


class AgentWriteSettings(SectionModel):
    """Writes of external agents through governed Operations (design 10.1, 11.1, 13.2).

    When an Operation needs review (protected target branch, or its review policy) and the
    caller named a `task`, Keeper runs it on the task branch `{branch_prefix}/{principal}/
    {task}` and opens or updates a Proposal instead of answering `review-required`. A Domain
    overrides any member here with the same name in its `agent_writes` setting.
    """

    enabled: bool = Field(
        default=True, description="Turn Operations that need review into task branches."
    )
    principal_types: tuple[PrincipalType, ...] = Field(
        default=(PrincipalType.SERVICE,),
        description="Principal types that are agents: for them `auto_propose` applies when a "
        "request leaves it out; other principals must ask with `auto_propose: true`.",
    )
    auto_propose: bool = Field(
        default=True,
        description="Open a Proposal when a request of an agent that names a `task` needs "
        "review and does not set `auto_propose`.",
    )
    auto_merge: bool = Field(
        default=False,
        description="Merge the Proposal at once when a request does not set `auto_merge`. "
        "Needs a role in `allowed_roles_for_auto_merge`.",
    )
    allowed_roles_for_auto_merge: tuple[str, ...] = Field(
        default=(),
        description="Roles (granted in the Domain, covering the task branch) whose agents may "
        "have their Proposals merged automatically; empty: nobody. Approvals the target "
        "branch requires are never skipped.",
    )
    max_open_branches_per_principal: int = Field(
        default=10, ge=1, description="Task branches one principal may hold at a time."
    )
    branch_prefix: str = Field(
        default="agent",
        pattern=DEFAULT_REF_NAME_PATTERN,
        description="First part of task branch names.",
    )
    branch_mode: Literal["copy", "overlay"] = Field(
        default="overlay",
        description="How task branches are materialized: overlay (only the rows the task "
        "changes, reading the target as of the fork point; a copy when the target is not "
        "fully materialized) or copy (full rows).",
    )
    delete_branch_on_merge: bool = Field(
        default=True,
        description="Delete a task branch when its Proposal merged or was closed and no other "
        "open Proposal uses it.",
    )
    max_task_length: int = Field(
        default=60, ge=1, description="Longest `task` (after normalization) accepted."
    )
    title_template: str = Field(
        default="Agent {principal}: {task}",
        min_length=1,
        description="Title of the Proposal; `{principal}` and `{task}` are replaced.",
    )
    summary_parameter_values: bool = Field(
        default=True,
        description="List parameter values in the Proposal description; false lists names only "
        "(values may be sensitive).",
    )
    summary_value_chars: int = Field(
        default=80, ge=1, description="Characters of a parameter value kept in the description."
    )
    max_summary_operations: int = Field(
        default=20, ge=1, description="Executions listed in the description of one Proposal."
    )

    @field_validator("title_template")
    @classmethod
    def _known_placeholders(cls, value: str) -> str:
        try:
            value.format(principal="p", task="t")
        except (KeyError, IndexError, ValueError):
            raise ValueError("only {principal} and {task} may be used as placeholders") from None
        return value


DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class DocumentSettings(SectionModel):
    """Document intake for extraction (design 9.2 step 1): uploads and allowlisted URLs.

    The upload route accepts bodies up to `max_bytes` plus `multipart_overhead_bytes`,
    whatever `keeper.limits.max_request_body_bytes` says.
    """

    max_bytes: int = Field(
        default=20 * 1024 * 1024, ge=1, description="Largest document, in bytes (413 above)."
    )
    multipart_overhead_bytes: int = Field(
        default=64 * 1024, ge=0, description="Room for multipart framing and form fields."
    )
    media_types: dict[str, str] = Field(
        default_factory=lambda: {
            ".md": "text/markdown",
            ".markdown": "text/markdown",
            ".txt": "text/plain",
            ".pdf": "application/pdf",
            ".docx": DOCX_MEDIA_TYPE,
        },
        min_length=1,
        description="Accepted file name suffixes and the media type each one is stored as.",
    )
    url_hosts: tuple[str, ...] = Field(
        default=(),
        description="Host names documents may be fetched from (exact match); empty disables "
        "URL intake. Keeper fetches them itself, without following redirects.",
    )
    url_schemes: tuple[str, ...] = Field(
        default=("https",), min_length=1, description="URL schemes allowed for URL intake."
    )
    url_timeout_s: float = Field(default=30.0, gt=0, description="Time budget of one fetch.")
    extract_on_upload: bool = Field(
        default=True,
        description="Queue an extraction job for every uploaded document unless the request "
        "says otherwise.",
    )
    jobs_listed: int = Field(
        default=20, ge=1, description="Extraction jobs listed with a document, newest first."
    )
    max_text_chars: int = Field(
        default=2_000_000,
        ge=1,
        description="Characters of parsed text one `GET .../documents/{id}/text` returns; a "
        "longer text is cut there and the answer says `truncated`.",
    )
