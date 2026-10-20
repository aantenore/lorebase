"""Deterministic schema inference from structured sources (extraction tier 0, design 9.1).

``infer_schema`` turns ``TableProfile`` objects (columns with sampled or complete values, row
counts and whatever a catalog declares) into a ``SchemaProposal``: a draft ``Schema`` with Kinds,
typed Attributes with constraints, primary keys and Relations with cardinality, the evidence
behind each element and the issues a reviewer must settle. No model is called and nothing is
read or written: profilers (CSV, database catalogs) live outside the core.

Thresholds and naming rules are ``InferenceOptions``; see docs/inference.md.
"""

from lorebase_core.infer.engine import infer_schema
from lorebase_core.infer.options import InferenceOptions
from lorebase_core.infer.profile import ColumnProfile, DeclaredForeignKey, TableProfile
from lorebase_core.infer.proposal import (
    Evidence,
    InferenceIssue,
    InferenceIssueCode,
    SchemaProposal,
    TableOutcome,
    TableRole,
)

__all__ = [
    "ColumnProfile",
    "DeclaredForeignKey",
    "Evidence",
    "InferenceIssue",
    "InferenceIssueCode",
    "InferenceOptions",
    "SchemaProposal",
    "TableOutcome",
    "TableProfile",
    "TableRole",
    "infer_schema",
]
