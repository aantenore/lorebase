"""Extraction Proposals without models: item edits, materialization on the extraction branch,
the Proposal summary (design 9.2 steps 11 and 12). Scribe produces the items; Keeper records
review decisions; both materialize through this package."""

from lorebase_store.extraction.items import (
    ItemEditError,
    effective_subject,
    link_element,
    normalize_edits,
    proposal_text,
    record_element,
    summarize,
)
from lorebase_store.extraction.materialize import (
    EXTRACTION_ORIGIN,
    RULES_MODEL,
    MaterializeContext,
    Materialized,
    materialize_items,
)
from lorebase_store.extraction.proposal import (
    EXTRACTION_CHECK,
    extraction_check,
    refresh_proposal,
)

EXTRACTION_BRANCH_PREFIX = "extract/"
"""Extraction branches are named `extract/<extraction job id>`."""

__all__ = [
    "EXTRACTION_BRANCH_PREFIX",
    "EXTRACTION_CHECK",
    "EXTRACTION_ORIGIN",
    "RULES_MODEL",
    "ItemEditError",
    "MaterializeContext",
    "Materialized",
    "effective_subject",
    "extraction_check",
    "link_element",
    "materialize_items",
    "normalize_edits",
    "proposal_text",
    "record_element",
    "refresh_proposal",
    "summarize",
]
