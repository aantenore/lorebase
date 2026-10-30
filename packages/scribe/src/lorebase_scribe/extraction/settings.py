"""The `scribe.extraction` settings: tiers, chunking, entity resolution, confidence, budgets.

Models themselves (chat model, span and relation extractors) are configured in the `inference`
section; this section decides how Scribe uses them.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from lorebase_platform.settings import SectionModel
from lorebase_store.extraction.modes import Ingestion, Installation

Tier = Literal[0, 1, 2]
IngestionMode = Literal["review_all", "doubts_only", "direct"]
LoosenableMember = Literal["mode", "auto_accept_threshold", "min_confidence"]


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


class ParserSettings(SectionModel):
    """Limits of the process that turns PDF and DOCX bytes into text (design 9.2 step 2).

    A crafted file can make a parser loop or allocate without bound, so PDF and DOCX are parsed
    in a child process with no network access, limited in wall-clock time, CPU time and address
    space; a document over `max_input_bytes` is not parsed at all. A document that breaks a
    limit fails its extraction job with a message naming the setting. Markdown and plain text
    are decoded in the worker under the same size limit.
    """

    isolated: bool = Field(
        default=True,
        description="Parse PDF and DOCX in a child process with the limits below. Off: in a "
        "worker thread, bounded only by max_input_bytes.",
    )
    timeout_s: float = Field(
        default=60.0, gt=0, description="Wall-clock time a document may take to parse."
    )
    cpu_s: int = Field(
        default=60, ge=1, description="CPU seconds of the parser process (RLIMIT_CPU, POSIX)."
    )
    memory_mb: int = Field(
        default=1024,
        ge=128,
        description="Address space of the parser process in MiB (RLIMIT_AS; Linux and other "
        "POSIX systems that enforce it).",
    )
    max_input_bytes: int = Field(
        default=50 * 1024 * 1024, ge=1, description="Largest document parsed, in bytes."
    )


class DiscoverySettings(SectionModel):
    """Schema suggestions (design 9.2 step 10): what extraction reports when it finds types,
    fields or relations the schema does not have. How suggestions are aggregated, listed and
    matched against the schema is set in `schema.suggestions`."""

    enabled: bool = Field(
        default=True, description="Record schema suggestions from extraction jobs."
    )
    rules: bool = Field(
        default=True,
        description="Tier 0: `**Label:** value` field lines and table columns no Attribute "
        "matches, and headings naming a thing by a code no Kind's key pattern knows.",
    )
    span_labels: tuple[str, ...] = Field(
        default=(),
        description="Tier 1: extra labels for the span extractor; spans found with them "
        "suggest a Kind of that name (e.g. certificate, warehouse).",
    )
    relation_labels: tuple[str, ...] = Field(
        default=(),
        description="Tier 1: extra labels for the relation extractor; relations found with "
        "them between Records of known Kinds suggest a Relation of that name.",
    )
    tier2: bool = Field(
        default=True,
        description="Tier 2: the chat model may report facts outside the schema (`unlisted`) "
        "with verified evidence, and its answers outside the schema are kept as observations.",
    )
    llm_pass: bool = Field(
        default=False,
        description="Run a dedicated discovery call per chunk through the "
        "`inference.llm.tasks.schema_discovery` route (any chat model; the extraction route "
        "when the task has no override) that proposes Kinds and Attributes from the document "
        "text. Needs no tier 2. It runs after tier 2 of each document and has its own token cap "
        "(`llm_max_tokens`), so it never takes tokens from `max_tokens_per_job`.",
    )
    llm_max_tokens: int = Field(
        default=200_000,
        ge=0,
        description="The dedicated discovery calls stop once a job used this many tokens "
        "(input and output, estimated before each call); 0 means no limit. Separate from "
        "`max_tokens_per_job`, which tier 2 alone spends.",
    )
    llm_max_quote_chars: int = Field(
        default=300,
        ge=16,
        description="A proposal of the dedicated call whose evidence quote is longer is discarded.",
    )
    llm_max_quote_share: float = Field(
        default=0.5,
        gt=0,
        le=1,
        description="A proposal whose evidence quote covers more than this share of the "
        "chunk is discarded: it quotes the text instead of pointing at a fact. Applies only to "
        "chunks of at least `llm_quote_share_min_chunk_chars` characters.",
    )
    llm_quote_share_min_chunk_chars: int = Field(
        default=400,
        ge=0,
        description="Shorter chunks are exempt from `llm_max_quote_share`: a legitimate quote "
        "can cover most of a short chunk.",
    )
    llm_max_items: int = Field(
        default=20, ge=1, description="Proposals of each list the dedicated call may return."
    )
    min_confidence: float = Field(
        default=0.3, ge=0, le=1, description="Weaker observations are dropped."
    )
    field_confidence: float = Field(
        default=0.6, ge=0, le=1, description="Confidence of a field line or table column."
    )
    heading_confidence: float = Field(
        default=0.5, ge=0, le=1, description="Confidence of a Kind named by a heading."
    )
    max_value_chars: int = Field(
        default=120, ge=8, description="Sample values are cut to this length."
    )
    vault_match_max_words: int = Field(
        default=12,
        ge=1,
        le=64,
        description="Longest word sequence of a quote compared with the keyed fingerprints of "
        "vaulted values of any Kind; a quote holding one is kept without its evidence and sample.",
    )
    vault_match_max_text_words: int = Field(
        default=400,
        ge=1,
        description="A quote or sample with more words is not compared and is treated as "
        "holding a vaulted value (its evidence is withheld).",
    )
    vault_match_max_text_chars: int = Field(
        default=4000,
        ge=1,
        description="Same, by characters.",
    )
    vault_match_max_candidates: int = Field(
        default=200_000,
        ge=1,
        description="Distinct word sequences compared per job; past it the remaining quotes "
        "are treated as holding a vaulted value.",
    )
    max_observations_per_job: int = Field(
        default=1000, ge=1, description="Observations beyond this many are dropped and counted."
    )


class VersionCheckSettings(SectionModel):
    """The evidence check of a new document version or a retired document (job
    `document_version_check`, design 9.2)."""

    max_values_per_job: int = Field(
        default=5000,
        ge=1,
        description="Values one document_version_check job looks at; the rest goes to a "
        "continuation job.",
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
    mode: IngestionMode = Field(
        default="doubts_only",
        description="Ingestion mode of Domains without their own (design 9.2): review_all "
        "accepts nothing at once, doubts_only accepts confident items and leaves the doubts "
        "to reviewers. direct is refused here: a Domain may choose it, never the installation.",
    )
    domain_may_loosen: tuple[LoosenableMember, ...] = Field(
        default=("mode",),
        description="Members a Domain's settings.extraction may set looser than the values "
        "above (a looser mode, a lower auto_accept_threshold, a higher min_confidence); "
        "tightening is always allowed. An empty list forbids loosening.",
    )
    direct_merge_protected: bool = Field(
        default=True,
        description="Whether a direct-mode job may merge its Proposal into a protected branch, "
        "waiving the required_approvals count but not steward approvals or required roles "
        "(keeper.proposals, Domain overrides); off, such Proposals wait for their approvals.",
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
    parser: ParserSettings = Field(default_factory=ParserSettings)
    chunking: ChunkSettings = Field(default_factory=ChunkSettings)
    matching: MatchSettings = Field(default_factory=MatchSettings)
    confidence: ConfidenceSettings = Field(default_factory=ConfidenceSettings)
    discovery: DiscoverySettings = Field(default_factory=DiscoverySettings)
    versions: VersionCheckSettings = Field(default_factory=VersionCheckSettings)

    @model_validator(mode="after")
    def _direct_is_per_domain(self) -> Self:
        if self.mode == "direct":
            raise ValueError(
                "mode: direct is refused for the installation; set it in a Domain's "
                "settings.extraction (design 9.2)"
            )
        return self

    def installation(self) -> Installation:
        """The bounds Domains' extraction settings are resolved against."""
        return Installation(
            base=Ingestion(
                mode=self.mode,
                auto_accept_threshold=self.auto_accept_threshold,
                min_confidence=self.min_confidence,
            ),
            domain_may_loosen=frozenset(self.domain_may_loosen),
        )
