"""Response shapes of the health reports. Names are api names; no Attribute value is ever
printed except the normalized value of a duplicate on an Attribute without sensitivity tags.
For Attributes whose values the caller may not read, validation issues carry the generic
`value.invalid` code and duplicate groups are only counted (`DuplicateReport.hidden`)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from lorebase_core.model.relation import RelationBacking
from lorebase_keeper.reading.views import Served


class IssueGroup(BaseModel):
    """Records failing the same rule of the current schema."""

    code: str = Field(
        description="Validation issue code, e.g. value.required or value.pattern; value.invalid "
        "for an Attribute whose values the caller may not read."
    )
    attribute: str | None = Field(default=None, description="Attribute api name, when one.")
    count: int
    examples: list[str] = Field(description="Keys of some of the Records.")


class ViolationReport(BaseModel):
    """Result of re-validating Records with the schema of the branch head."""

    checked: int = Field(description="Records re-validated.")
    sampled: bool = Field(description="Only a subset of the Kind's Records was re-validated.")
    invalid_records: int = Field(description="Re-validated Records with at least one issue.")
    issues: list[IssueGroup]
    issues_truncated: bool = False


class DuplicateGroup(BaseModel):
    """Records whose value of one Attribute is the same once case and spacing are ignored."""

    attribute: str
    unique: bool = Field(description="The Attribute is unique: this is a violation, not a hint.")
    value: str | None = Field(
        description="The normalized value; null for Attributes with sensitivity tags."
    )
    count: int
    keys: list[str]


class HiddenDuplicates(BaseModel):
    """Duplicates on an Attribute whose values the caller may not read: counted, not listed
    (which Records share a value would tell that their values are equal)."""

    attribute: str
    unique: bool = Field(description="The Attribute is unique: this is a violation, not a hint.")
    groups: int = Field(description="Groups of Records sharing a value.")
    records: int = Field(description="Records in those groups.")


class DuplicateReport(BaseModel):
    """Normalized exact duplicates on unique and title Attributes."""

    attributes: list[str] = Field(description="Attributes checked.")
    groups: list[DuplicateGroup]
    total_groups: int = Field(description="Groups found, listed or not (`hidden` included).")
    skipped: Literal["too_many_records", "disabled"] | None = None
    hidden: list[HiddenDuplicates] = Field(
        default_factory=list,
        description="Attributes with duplicates whose groups are not listed: the caller is "
        "not cleared for their values.",
    )


class DerivedReport(BaseModel):
    """Derived Attributes whose materialized value is missing, so reads cannot serve it.

    Only presence is checked: values are not recomputed and compared with the stored ones
    (`lorebase_store.materialize.verify` with `derived=True` does that).
    """

    attributes: list[str]
    records_missing_value: int
    stale: bool


class LastChange(BaseModel):
    """The latest Revision on the branch that changed a Record of the Kind."""

    revision: str
    at: datetime


class LinkRef(BaseModel):
    from_key: str
    to_key: str


class OrphanLinks(BaseModel):
    """Links, or foreign keys, whose endpoint Record does not exist."""

    count: int
    missing_from: int = Field(description="Links whose `from` Record is missing.")
    missing_to: int = Field(description="Links whose `to` Record is missing.")
    examples: list[LinkRef]


class RequiredGaps(BaseModel):
    """Records of the `from` Kind without the Link a required Relation demands."""

    missing: int
    examples: list[str] = Field(description="Keys of some of the Records.")


class RelationHealth(BaseModel):
    relation: str = Field(description="Relation api name, forward direction.")
    from_kind: str
    to_kind: str
    backing: RelationBacking
    links: int
    orphan_links: OrphanLinks
    required: bool
    required_gaps: RequiredGaps | None = Field(
        default=None, description="Present for Relations with `required_from`."
    )
    direction: Literal["outgoing", "incoming", "self"] | None = Field(
        default=None, description="In a Kind report: the side the Kind is on."
    )


class KindHealth(BaseModel):
    kind: str
    records: int
    last_change: LastChange | None = None
    violations: ViolationReport
    duplicates: DuplicateReport
    derived: DerivedReport | None = Field(
        default=None, description="Present for Kinds with derived Attributes."
    )


class HealthSummary(BaseModel):
    ok: bool = Field(
        description="No violations, orphan Links, required-relation gaps or duplicates of unique "
        "Attributes found. Duplicate candidates on title Attributes and stale derived values "
        "are reported but do not make a report not ok."
    )
    kinds: int
    records: int
    links: int
    invalid_records: int
    orphan_links: int
    required_relation_gaps: int
    duplicate_groups: int
    stale_kinds: int = Field(description="Kinds with derived values missing.")
    sampled_kinds: int = Field(description="Kinds re-validated on a sample only.")


class DomainHealthOut(Served):
    """Health of a Domain at the head of a branch, for the Kinds the caller may read."""

    domain: str
    branch: str
    summary: HealthSummary
    kinds: list[KindHealth]
    relations: list[RelationHealth]


class KindHealthOut(Served):
    """Health of one Kind, with the Relations it takes part in."""

    domain: str
    branch: str
    summary: HealthSummary
    health: KindHealth
    relations: list[RelationHealth]
