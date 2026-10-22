"""The `read` and `search` settings sections (design 8, 16), registered with the platform.

`read` bounds every read: page sizes, query complexity, traversal caps, time travel, diff and
log pages, and the statement timeout of read transactions. `search` holds the ranking knobs of
lexical, semantic and hybrid search. The text search configuration is the one the vectors were
built with (`store.materialize.text_search_config`) and the vector dimension is
`store.embeddings.dims`, so neither is repeated here.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from lorebase_core.query import QueryLimits
from lorebase_keeper.kindhealth.config import HealthSettings
from lorebase_platform.settings import SectionModel, Settings, register_section

READ_SECTION = "read"
SEARCH_SECTION = "search"


class Bounds(SectionModel):
    """A page size default and its maximum."""

    default_limit: int = Field(default=50, ge=1)
    max_limit: int = Field(default=500, ge=1)

    @model_validator(mode="after")
    def _default_within_max(self) -> Self:
        if self.default_limit > self.max_limit:
            raise ValueError("default_limit cannot exceed max_limit")
        return self


class QuerySettings(SectionModel):
    """Complexity limits of the query DSL."""

    max_via_depth: int = Field(default=3, ge=1, description="Nesting of via/count predicates.")
    max_predicates: int = Field(default=64, ge=1, description="Predicates per query.")
    max_in_values: int = Field(default=1000, ge=1, description="Operands of one `in`.")
    max_order_terms: int = Field(default=4, ge=1, description="Terms of `order_by`.")
    max_includes: int = Field(default=5, ge=0, description="Relations in `include`.")
    include_default_limit: int = Field(default=10, ge=1, description="Neighbors per Record.")
    include_max_limit: int = Field(default=50, ge=1)
    max_aggregate_groups: int = Field(
        default=1000, ge=1, description="Groups returned by `aggregate`; more are cut."
    )


class TraverseSettings(SectionModel):
    """`POST /traverse` and `POST /neighborhood` (design 8.3)."""

    max_hops: int = Field(default=5, ge=1, description="Relations in one traversal path.")
    max_nodes_per_hop: int = Field(default=1000, ge=1, description="Records reached per hop.")
    max_seeds: int = Field(default=100, ge=1, description="Seed Records of a neighborhood.")
    max_depth: int = Field(default=3, ge=1, description="Depth of a neighborhood.")
    max_nodes: int = Field(default=2000, ge=1, description="Records in a neighborhood.")
    max_edges: int = Field(default=5000, ge=1, description="Links in a neighborhood.")


class TimeTravelSettings(SectionModel):
    """Reads at an arbitrary Revision, served from the tree (design 6.12)."""

    max_scan_entries: int = Field(
        default=100_000,
        ge=1,
        description="Largest Kind or Relation partition a list read at a Revision scans; "
        "larger ones are refused (read the materialized branch instead).",
    )
    snapshot_max_entries: int = Field(
        default=100_000,
        ge=0,
        description="Most Records and Links a Revision may hold for reads at it that need "
        "materialized rows (filters, ordering, includes, aggregates, Link summaries, "
        "traversal, search, history, health, context packs); they are served from a snapshot "
        "of the Revision, and refused above this size. 0 refuses them all.",
    )
    snapshot_max_history_revisions: int = Field(
        default=100_000,
        ge=1,
        description="Most Revisions in the history of a Revision (its generation + 1, a bound "
        "on its first-parent chain, whose changes a snapshot copies); deeper ones are refused.",
    )
    snapshot_max_per_domain: int = Field(
        default=4,
        ge=1,
        description="Snapshots a Domain holds, those being deleted included. Building another "
        "first drops the oldest that no read holds and that are older than "
        "`snapshot_min_age_s`; when none can go, the read answers 503 with Retry-After.",
    )
    snapshot_min_age_s: int = Field(
        default=60,
        ge=0,
        description="Grace period of a new snapshot: younger ones are not dropped for others.",
    )
    snapshot_max_concurrent_builds: int = Field(
        default=2,
        ge=1,
        description="Snapshot builds running at once in this process. Each holds a second "
        "database connection besides its read's, so keep it well below the pool size; builds "
        "of one Domain also run one at a time.",
    )
    snapshot_build_wait_ms: int = Field(
        default=5000,
        ge=0,
        description="How long a read waits for a build slot or for the Domain's running build "
        "before answering 503 with Retry-After.",
    )
    snapshot_builds_per_minute: float = Field(
        default=6,
        gt=0,
        description="Snapshot builds one principal may start per minute in this process "
        "(token bucket); beyond it reads that need a new snapshot answer 429.",
    )
    snapshot_build_burst: int = Field(
        default=3, ge=1, description="Snapshot builds one principal may start at once."
    )


class DiffSettings(SectionModel):
    """Diff of two Revisions (design 6.7)."""

    max_changes: int = Field(
        default=100_000, ge=1, description="Changed elements a diff may hold; larger ones fail."
    )
    page: Bounds = Field(default_factory=lambda: Bounds(default_limit=100, max_limit=1000))


class HistorySettings(Bounds):
    """Per-Record history and lineage (`.../records/{key}/history` and `/lineage`)."""

    lineage_scan_changes: int = Field(
        default=1000,
        ge=1,
        description="Newest changes of a Record lineage searches for the Revision behind a "
        "value, or the change that created the Record, when the materialized row does not name "
        "them (rows written before migration 0032 and not yet backfilled); older ones name no "
        "Revision.",
    )
    lineage_count_max: int = Field(
        default=10_000,
        ge=1,
        description="Changes of a Record lineage counts; beyond, `changes` is this bound and "
        "`changes_truncated` is true.",
    )


class ReadSettings(SectionModel):
    """Bounds of the read path."""

    records: Bounds = Field(default_factory=Bounds, description="Record and Link pages.")
    log: Bounds = Field(default_factory=Bounds, description="Pages of the Revision log.")
    history: HistorySettings = Field(
        default_factory=HistorySettings, description="Per-Record history and lineage."
    )
    statement_timeout_ms: int = Field(
        default=2000, ge=0, description="statement_timeout of read transactions; 0 disables."
    )
    schema_cache_entries: int = Field(
        default=64, ge=1, description="Compiled schemas kept in memory, by schema hash."
    )
    derivation_preview: Bounds = Field(
        default_factory=lambda: Bounds(default_limit=5, max_limit=25),
        description="Records `POST /derivations:preview` evaluates a candidate Derivation on: "
        "the default and the most one request may ask for.",
    )
    derivation_preview_max_key_length: int = Field(
        default=1024,
        ge=1,
        description="Characters of each Record key `POST /derivations:preview` accepts in `keys`.",
    )
    source_preview_max_rows: int = Field(
        default=100,
        ge=1,
        description="Sample rows `POST /sources:preview` evaluates the expression mappings of a "
        "candidate Source on.",
    )
    source_preview_max_columns: int = Field(
        default=200,
        ge=1,
        description="Columns of one sample row `POST /sources:preview` accepts.",
    )
    source_preview_max_cells: int = Field(
        default=1000,
        ge=1,
        description="Expressions evaluated by one `POST /sources:preview`: sample rows times "
        "Attributes mapped by an expression.",
    )
    query: QuerySettings = Field(default_factory=QuerySettings)
    traverse: TraverseSettings = Field(default_factory=TraverseSettings)
    time_travel: TimeTravelSettings = Field(default_factory=TimeTravelSettings)
    diff: DiffSettings = Field(default_factory=DiffSettings)
    health: HealthSettings = Field(default_factory=HealthSettings)

    def query_limits(self) -> QueryLimits:
        """Limits handed to the DSL resolver."""
        query = self.query
        return QueryLimits(
            default_limit=self.records.default_limit,
            max_limit=self.records.max_limit,
            max_via_depth=query.max_via_depth,
            max_predicates=query.max_predicates,
            max_in_values=query.max_in_values,
            include_default_limit=query.include_default_limit,
            include_max_limit=query.include_max_limit,
            max_includes=query.max_includes,
            max_order_terms=query.max_order_terms,
        )


class SemanticSettings(SectionModel):
    """Vector search over `embeddings`. Query vectors always come from the caller."""

    model: str = Field(
        default="BAAI/bge-small-en-v1.5",
        min_length=1,
        description="Embedding model whose vectors are searched; equals "
        "inference.embeddings.model. The dimension is store.embeddings.dims.",
    )


class HybridSettings(SectionModel):
    """Reciprocal rank fusion of the lexical and semantic lists (design 8.4)."""

    rrf_k: int = Field(default=60, ge=1, description="Rank offset of reciprocal rank fusion.")
    lexical_weight: float = Field(default=1.0, ge=0)
    semantic_weight: float = Field(default=1.0, ge=0)


class PartialMatchSettings(SectionModel):
    """Ranked partial matching of lexical search: descriptive queries rarely have every word
    in one Record, so when the query as written (every term) finds too few, Records holding
    some of its terms follow, after every full match."""

    min_results: int = Field(
        default=1,
        ge=0,
        description="Full matches below which partial matches are added; 0 turns partial "
        "matching off.",
    )
    weight: float = Field(
        default=0.5,
        ge=0,
        description="Partial matches score weight * (ts_rank_cd of the terms joined by OR + "
        "term_bonus * terms matched).",
    )
    term_bonus: float = Field(
        default=1.0, ge=0, description="Score added for each query term a partial match holds."
    )
    candidates: int = Field(
        default=100,
        ge=1,
        description="Partial matches ranked per search, best by ts_rank_cd first; at most "
        "store.reads.max_search_rows without filters, search.candidates with them.",
    )
    stop_words: str | None = Field(
        default="english",
        min_length=1,
        description="Text search configuration whose stop words are left out of the terms of "
        "partial matching (the, for, that...): the search configuration, simple by default, "
        "keeps every word. null leaves only the search configuration's own stop words out.",
    )


class SearchSettings(SectionModel):
    """Ranking of `POST /search`."""

    results: Bounds = Field(default_factory=lambda: Bounds(default_limit=20, max_limit=100))
    candidates: int = Field(
        default=200, ge=1, description="Rows of each ranked list before fusion and paging."
    )
    trigram_threshold: float = Field(
        default=0.3,
        gt=0,
        le=1,
        description="Least trigram similarity between a query word the branch does not hold "
        "and a word of the branch that corrects it.",
    )
    trigram_weight: float = Field(
        default=0.5,
        ge=0,
        description="Weight of ts_rank_cd for hits found only through a corrected word; they "
        "always rank after the hits of the query as written.",
    )
    typo_candidates: int = Field(
        default=3,
        ge=0,
        description="Corrections tried per unknown query word, most similar first; 0 turns "
        "typo tolerance off.",
    )
    typo_min_length: int = Field(
        default=4, ge=1, description="Shortest query word (in characters) that is corrected."
    )
    headline_options: str = Field(
        default="MaxWords=30, MinWords=10, MaxFragments=2",
        min_length=1,
        description="ts_headline options of result snippets.",
    )
    partial: PartialMatchSettings = Field(default_factory=PartialMatchSettings)
    semantic: SemanticSettings = Field(default_factory=SemanticSettings)
    hybrid: HybridSettings = Field(default_factory=HybridSettings)


register_section(READ_SECTION, ReadSettings)
register_section(SEARCH_SECTION, SearchSettings)


def read_settings(settings: Settings) -> ReadSettings:
    """The validated `read` section."""
    return settings.section(READ_SECTION, ReadSettings)


def search_settings(settings: Settings) -> SearchSettings:
    """The validated `search` section."""
    return settings.section(SEARCH_SECTION, SearchSettings)
