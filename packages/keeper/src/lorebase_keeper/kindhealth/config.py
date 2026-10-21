"""Settings of health reports (`read.health`)."""

from __future__ import annotations

from pydantic import Field

from lorebase_platform.settings import SectionModel


class HealthSettings(SectionModel):
    """Caps of `GET /domains/{d}/health` and `GET /domains/{d}/kinds/{kind}/health`.

    A report is computed when asked for, from the materialized head of a branch, and kept in
    the response cache under the Revision it describes. The caps keep one request affordable on
    large Domains; what a cap cuts is reported (`sampled`, `skipped`, `truncated`).
    """

    max_records_validated: int = Field(
        default=5000,
        ge=1,
        description="Records of one Kind re-validated against the current schema. A larger "
        "Kind is sampled: a fixed, evenly spread subset of about this size, the same one for "
        "the same data.",
    )
    examples_per_issue: int = Field(
        default=3, ge=0, description="Record keys listed as examples of each kind of violation."
    )
    max_issue_groups: int = Field(
        default=50, ge=1, description="Kinds of violation listed per Kind; the rest are cut."
    )
    duplicates_max_records: int = Field(
        default=200_000,
        ge=0,
        description="Kinds with more Records are not scanned for duplicates (reported as "
        "skipped). 0 turns the duplicate check off.",
    )
    max_duplicate_groups: int = Field(
        default=20, ge=1, description="Duplicate groups listed per Attribute, most Records first."
    )
    duplicate_keys_per_group: int = Field(
        default=5, ge=1, description="Record keys listed in a duplicate group."
    )
    orphan_examples: int = Field(
        default=5, ge=0, description="Orphan Links and Records with a gap listed per Relation."
    )
    statement_timeout_ms: int = Field(
        default=15_000,
        ge=0,
        description="statement_timeout of the scans, instead of `read.statement_timeout_ms`; "
        "0 disables. A scan that exceeds it is answered 503.",
    )
