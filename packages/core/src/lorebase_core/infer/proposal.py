"""Output of schema inference: the proposed ``Schema`` with evidence and issues to review."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import JsonValue

from lorebase_core.model.schema import Schema
from lorebase_core.validate.issues import Severity


class InferenceIssueCode(StrEnum):
    """Stable codes of what a reviewer should look at."""

    AMBIGUOUS_PRIMARY_KEY = "infer.ambiguous_primary_key"
    NO_PRIMARY_KEY = "infer.no_primary_key"
    MIXED_TYPES = "infer.mixed_types"
    AMBIGUOUS_REFERENCE = "infer.ambiguous_reference"
    UNSUPPORTED_REFERENCE = "infer.unsupported_reference"
    EMPTY_TABLE = "infer.empty_table"
    EMPTY_COLUMN = "infer.empty_column"
    NAME_ADJUSTED = "infer.name_adjusted"
    SAMPLED = "infer.sampled"
    SCHEMA_INVALID = "infer.schema_invalid"


@dataclass(frozen=True, slots=True)
class Evidence:
    """Why one element was proposed.

    ``element`` is a path into the schema document (``kinds.Supplier.attributes.country``,
    ``relations.supplier``); ``source`` names the table or ``table.column`` it came from.
    """

    element: str
    element_id: str
    source: str
    confidence: float
    reasons: tuple[str, ...] = ()
    samples: tuple[str, ...] = ()

    def document(self) -> dict[str, JsonValue]:
        return {
            "element": self.element,
            "element_id": self.element_id,
            "source": self.source,
            "confidence": self.confidence,
            "reasons": list(self.reasons),
            "samples": list(self.samples),
        }


@dataclass(frozen=True, slots=True)
class InferenceIssue:
    """An ambiguity or a gap the reviewer must settle."""

    code: InferenceIssueCode
    message: str
    severity: Severity = Severity.WARNING
    element: str | None = None
    source: str | None = None
    details: dict[str, JsonValue] = field(default_factory=dict)

    def document(self) -> dict[str, JsonValue]:
        return {
            "code": self.code.value,
            "severity": self.severity.value,
            "message": self.message,
            "element": self.element,
            "source": self.source,
            "details": dict(self.details),
        }


class TableRole(StrEnum):
    KIND = "kind"
    RELATION = "relation"


@dataclass(frozen=True, slots=True)
class TableOutcome:
    """What a source table became."""

    table: str
    role: TableRole
    element: str
    rows: int
    values_read: int

    def document(self) -> dict[str, JsonValue]:
        return {
            "table": self.table,
            "role": self.role.value,
            "element": self.element,
            "rows": self.rows,
            "values_read": self.values_read,
        }


@dataclass(frozen=True)
class SchemaProposal:
    """A proposed schema to review; never committed by inference itself."""

    schema: Schema
    evidence: tuple[Evidence, ...]
    issues: tuple[InferenceIssue, ...]
    tables: tuple[TableOutcome, ...]

    def evidence_for(self, element: str) -> Evidence | None:
        return next((e for e in self.evidence if e.element == element), None)

    def document(self) -> dict[str, Any]:
        """JSON form: the schema content plus evidence, issues and table outcomes."""
        return {
            "schema": self.schema.content(),
            "evidence": [e.document() for e in self.evidence],
            "issues": [i.document() for i in self.issues],
            "tables": [t.document() for t in self.tables],
        }
