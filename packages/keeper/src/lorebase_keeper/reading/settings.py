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


class DiffSettings(SectionModel):
    """Diff of two Revisions (design 6.7)."""

    max_changes: int = Field(
        default=100_000, ge=1, description="Changed elements a diff may hold; larger ones fail."
    )
    page: Bounds = Field(default_factory=lambda: Bounds(default_limit=100, max_limit=1000))


class ReadSettings(SectionModel):
    """Bounds of the read path."""

    records: Bounds = Field(default_factory=Bounds, description="Record and Link pages.")
    log: Bounds = Field(default_factory=Bounds, description="Pages of the Revision log.")
    statement_timeout_ms: int = Field(
        default=2000, ge=0, description="statement_timeout of read transactions; 0 disables."
    )
    schema_cache_entries: int = Field(
        default=64, ge=1, description="Compiled schemas kept in memory, by schema hash."
    )
    query: QuerySettings = Field(default_factory=QuerySettings)
    traverse: TraverseSettings = Field(default_factory=TraverseSettings)
    time_travel: TimeTravelSettings = Field(default_factory=TimeTravelSettings)
    diff: DiffSettings = Field(default_factory=DiffSettings)

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


class SearchSettings(SectionModel):
    """Ranking of `POST /search`."""

    results: Bounds = Field(default_factory=lambda: Bounds(default_limit=20, max_limit=100))
    candidates: int = Field(
        default=200, ge=1, description="Rows of each ranked list before fusion and paging."
    )
    trigram_threshold: float = Field(
        default=0.3, ge=0, le=1, description="Least title similarity for a typo-tolerant hit."
    )
    trigram_weight: float = Field(
        default=0.5, ge=0, description="Weight of title similarity added to ts_rank_cd."
    )
    headline_options: str = Field(
        default="MaxWords=30, MinWords=10, MaxFragments=2",
        min_length=1,
        description="ts_headline options of result snippets.",
    )
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
