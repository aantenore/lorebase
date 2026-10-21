"""The `scribe.extraction` settings: tiers, chunking, entity resolution, confidence, budgets.

Models themselves (chat model, span and relation extractors) are configured in the `inference`
section; this section decides how Scribe uses them.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from lorebase_platform.settings import SectionModel

Tier = Literal[0, 1, 2]


class ChunkSettings(SectionModel):
    """Structure-aware chunks: paragraphs and headings are kept whole when they fit."""

    max_chars: int = Field(
        default=2000, ge=200, description="Largest chunk, in characters of parsed text."
    )
    overlap_chars: int = Field(
        default=200,
        ge=0,
        description="Characters a chunk repeats from the end of the previous one, so a fact "
        "cut by a boundary is still seen whole.",
    )

    @model_validator(mode="after")
    def _overlap_below_size(self) -> Self:
        if self.overlap_chars * 2 >= self.max_chars:
            raise ValueError("overlap_chars must be less than half of max_chars")
        return self


class MatchSettings(SectionModel):
    """Entity resolution of mentions against existing Records (design 9.2 step 9)."""

    trigram_threshold: float = Field(
        default=0.45, ge=0, le=1, description="Least trigram similarity of a fuzzy title match."
    )
    match_threshold: float = Field(
        default=0.6,
        ge=0,
        le=1,
        description="Least score for a mention to be matched to a Record; below, the mention "
        "is a new-Record candidate (when its Kind allows it) or dropped.",
    )
    ambiguity_margin: float = Field(
        default=0.05,
        ge=0,
        le=1,
        description="Candidates scoring within this margin of the best one make the match "
        "ambiguous; the reviewer picks.",
    )
    candidates: int = Field(default=5, ge=1, le=50, description="Candidates kept per mention.")
    dictionary_max_titles: int = Field(
        default=20000,
        ge=0,
        description="Titles of existing Records loaded per job for tier 0 dictionary matching; "
        "Kinds beyond the budget are matched by keys, tier 1 spans and trigrams only.",
    )
    min_title_chars: int = Field(
        default=4, ge=1, description="Shorter titles are not used for dictionary matching."
    )


class ConfidenceSettings(SectionModel):
    """Confidence given to candidates by the deterministic tiers and tier 2."""

    key: float = Field(default=1.0, ge=0, le=1, description="A key found by its pattern.")
    title: float = Field(default=0.95, ge=0, le=1, description="An exact title match.")
    normalized_title: float = Field(
        default=0.9, ge=0, le=1, description="A title equal after case and punctuation folding."
    )
    value_max: float = Field(
        default=0.85,
        ge=0,
        le=1,
        description="Ceiling of rule-detected Attribute values (closer cues score higher).",
    )
    cooccurrence_max: float = Field(
        default=0.7, ge=0, le=1, description="Ceiling of Links proposed from co-occurrence."
    )
    heading: float = Field(
        default=0.6,
        ge=0,
        le=1,
        description="Links between the Record a section heading names and the Records its "
        "text mentions.",
    )
    tier2: float = Field(
        default=0.8, ge=0, le=1, description="Facts returned by tier 2 with verified evidence."
    )


class ExtractionSettings(SectionModel):
    """Document extraction into reviewable Proposals (design 9.2)."""

    tiers: tuple[Tier, ...] = Field(
        default=(0, 1),
        min_length=1,
        description="Tiers a job runs unless the request names others: 0 rules and "
        "dictionaries, 1 local span and relation models, 2 the configured chat model. Add 2 "
        "once inference.llm points at a model.",
    )
    auto_accept_threshold: float = Field(
        default=0.9,
        ge=0,
        le=1.01,
        description="Items at or above this confidence are accepted when the Proposal opens "
        "and committed on the extraction branch; the rest stay pending for review. Above 1 "
        "accepts nothing automatically.",
    )
    min_confidence: float = Field(
        default=0.3, ge=0, le=1, description="Candidates below this confidence are dropped."
    )
    new_record_kinds: tuple[str, ...] | None = Field(
        default=None,
        description="Kind api names that may receive new-Record candidates; null allows every "
        "Kind with a natural key, an empty list allows none.",
    )
    value_cue_words: int = Field(
        default=8,
        ge=1,
        description="A rule-detected value counts only when a cue word of its Attribute "
        "precedes it by at most this many words in the sentence.",
    )
    max_items_per_job: int = Field(
        default=5000, ge=1, description="Items beyond this many are dropped and counted."
    )
    max_document_chars: int = Field(
        default=2_000_000, ge=1, description="Parsed text beyond this length is not extracted."
    )
    prompt_version: str = Field(
        default="extract-v1",
        min_length=1,
        description="Recorded in Lineage and in the cache key; change it when prompts change.",
    )
    cache: bool = Field(
        default=True, description="Reuse tier 2 answers for unchanged chunks, schema and model."
    )
    max_tokens_per_job: int = Field(
        default=2_000_000,
        ge=0,
        description="Tier 2 stops calling the model once a job used this many tokens; 0 means "
        "no limit.",
    )
    chunking: ChunkSettings = Field(default_factory=ChunkSettings)
    matching: MatchSettings = Field(default_factory=MatchSettings)
    confidence: ConfidenceSettings = Field(default_factory=ConfidenceSettings)
